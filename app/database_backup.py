"""Private SQLite online backup and isolated restore verification."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time


def readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('backup directory must be private (0700) and not a symlink')


def metadata(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {'snapshots', 'rank_snapshots', 'current_rank', 'student_roster'}
    if not required <= tables:
        raise ValueError('required application tables missing')
    latest = db.execute('SELECT id, fetched_at, user_count FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
    return {'latest_snapshot': list(latest) if latest else None,
            'snapshot_count': db.execute('SELECT count(*) FROM snapshots').fetchone()[0],
            'current_count': db.execute('SELECT count(*) FROM current_rank').fetchone()[0],
            'roster_count': db.execute('SELECT count(*) FROM student_roster').fetchone()[0]}


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_backup(source, output, *, max_seconds=1200, pause=0.05):
    source, output = Path(source).resolve(), Path(output).absolute()
    if output.exists() or output.is_symlink() or source == output.resolve():
        raise FileExistsError('output must be a new file distinct from source')
    private_directory(output.parent)
    if shutil.disk_usage(output.parent).free < source.stat().st_size * 2 + 256 * 1024**2:
        raise OSError('insufficient free space for backup and safety reserve')
    fd, name = tempfile.mkstemp(prefix='.backup-', suffix='.db', dir=output.parent)
    os.close(fd)
    temporary = Path(name)
    start = time.monotonic()
    last_report = start
    try:
        with readonly(source) as src, sqlite3.connect(temporary) as dst:
            src.execute('PRAGMA query_only=ON')
            src.execute('PRAGMA cache_size=-4096')
            if src.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
                raise ValueError('online backup requires WAL; do not hold a read transaction on rollback journal')
            # Pin a WAL read snapshot: concurrent commits do not restart the copy.
            # This retains WAL pages until completion, so a strict duration limit applies.
            src.execute('BEGIN')
            expected = metadata(src)
            dst.execute('PRAGMA cache_size=-4096')
            def progress(status, remaining, total):
                nonlocal last_report
                now = time.monotonic()
                if now - start > max_seconds:
                    raise TimeoutError('backup time budget exceeded')
                if now - last_report >= 30:
                    print(json.dumps({'phase': 'backup', 'pages_remaining': remaining,
                                      'pages_total': total}), flush=True)
                    last_report = now
                if remaining:
                    time.sleep(pause)
            src.backup(dst, pages=128, progress=progress, sleep=0.1)
            actual = metadata(dst)
            if expected != actual:
                raise ValueError('backup metadata mismatch')
            # Publish a standalone DB, with no dependency on backup-side WAL files.
            dst.execute('PRAGMA journal_mode=DELETE')
        # sqlite3 context managers commit but do not close connections.
        src.close()
        dst.close()
        with temporary.open('rb') as f:
            os.fsync(f.fileno())
        os.link(temporary, output)  # Atomic no-clobber publication.
        temporary.unlink()
        sync_directory(output.parent)
        return {'phase': 'backup_complete', 'bytes': output.stat().st_size, **actual}
    finally:
        for connection in (locals().get('src'), locals().get('dst')):
            if connection is not None:
                connection.close()
        for suffix in ('', '-wal', '-shm', '-journal'):
            Path(str(temporary) + suffix).unlink(missing_ok=True)


def verify_restore(backup, restore_dir, *, max_seconds=1200):
    backup, restore_dir = Path(backup).resolve(), Path(restore_dir).absolute()
    # A new directory prevents accidental replacement of any production DB.
    restore_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    restored = restore_dir / 'restored.db'
    started = time.monotonic()
    deadline = started + max_seconds
    digest = hashlib.sha256()
    try:
        if shutil.disk_usage(restore_dir).free < backup.stat().st_size + 256 * 1024**2:
            raise OSError('insufficient restore space')
        with backup.open('rb') as source, restored.open('xb') as target:
            os.chmod(restored, 0o600)
            while chunk := source.read(1024 * 1024):
                if time.monotonic() > deadline:
                    raise TimeoutError('restore time budget exceeded')
                target.write(chunk)
                digest.update(chunk)
            target.flush()
            os.fsync(target.fileno())
        with readonly(backup) as original, sqlite3.connect(restored) as recovered:
            expected = metadata(original)
            recovered.execute('PRAGMA cache_size=-4096')
            recovered.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            if recovered.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError('restored database quick_check failed')
            if metadata(recovered) != expected:
                raise ValueError('restore metadata mismatch')
            # Exercise writes only on the isolated copy, then roll them back.
            recovered.execute('BEGIN')
            recovered.execute('CREATE TABLE _restore_probe (value INTEGER)')
            recovered.execute('INSERT INTO _restore_probe VALUES (1)')
            recovered.rollback()
            if recovered.execute("SELECT 1 FROM sqlite_master WHERE name='_restore_probe'").fetchone():
                raise ValueError('restore rollback verification failed')
        original.close()
        recovered.close()
        result = {'phase': 'restore_verified', 'quick_check': 'ok', 'write_rollback': 'ok',
                  'backup_sha256': digest.hexdigest(), 'bytes': restored.stat().st_size, **expected}
        receipt = restore_dir / 'verification.json'
        receipt.write_text(json.dumps(result, indent=2) + '\n')
        receipt.chmod(0o600)
        return result
    finally:
        for connection in (locals().get('original'), locals().get('recovered')):
            if connection is not None:
                connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    b = sub.add_parser('backup')
    b.add_argument('--source', type=Path, required=True)
    b.add_argument('--output', type=Path, required=True)
    v = sub.add_parser('verify')
    v.add_argument('--backup', type=Path, required=True)
    v.add_argument('--restore-dir', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    os.nice(15)
    try:
        result = (create_backup(args.source, args.output) if args.command == 'backup'
                  else verify_restore(args.backup, args.restore_dir))
        print(json.dumps(result), flush=True)
        return 0
    except Exception as error:
        # Never print student records or SQL exception payloads.
        print(json.dumps({'phase': 'failed', 'error_type': type(error).__name__}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
