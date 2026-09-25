"""Daily local backups: verify before retention; never prune manual backups."""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
from datetime import datetime
from .database_backup import create_backup, verify_restore, private_directory, sync_directory
from .resource_guard import PressureGuard

MARKER = {'owner': 'oj-rank-scheduled-backup', 'version': 1}


def write_json(path, data):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)
    sync_directory(path.parent)


def owned(path):
    try:
        return (not path.is_symlink() and json.loads((path / 'managed.json').read_text()) == MARKER)
    except (OSError, ValueError):
        return False


def prune(root, keep=3):
    if keep < 1:
        raise ValueError('must retain at least one verified backup')
    verified = []
    for path in root.iterdir():
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', path.name) or not owned(path):
            continue
        try:
            receipt = json.loads((path / 'verified.json').read_text())
            if (receipt.get('phase') == 'restore_verified' and receipt.get('quick_check') == 'ok'
                    and receipt.get('write_rollback') == 'ok'
                    and (path/'rank.db').stat().st_size == receipt.get('bytes')
                    and {p.name for p in path.iterdir()} == {'rank.db', 'verified.json', 'managed.json'}
                    and not any(p.is_symlink() for p in path.iterdir())):
                verified.append(path)
        except (OSError, ValueError):
            continue
    removed = []
    for path in sorted(verified, reverse=True)[keep:]:
        for name in ('rank.db', 'verified.json', 'managed.json'):
            (path/name).unlink()
        path.rmdir()
        removed.append(path.name)
    sync_directory(root)
    return removed


def run_backup(source, root, day, keep=3):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise ValueError('invalid backup date')
    private_directory(root)
    with (root/'.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        target = root/day
        if not target.exists():
            target.mkdir(mode=0o700)
            write_json(target/'managed.json', MARKER)
        if not owned(target):
            raise ValueError('refusing unowned backup directory')
        if (target/'verified.json').exists():
            receipt = json.loads((target/'verified.json').read_text())
            if (receipt.get('phase') != 'restore_verified' or receipt.get('quick_check') != 'ok'
                    or receipt.get('write_rollback') != 'ok'
                    or receipt.get('bytes') != (target/'rank.db').stat().st_size):
                raise ValueError('invalid existing verification receipt')
        else:
            if shutil.disk_usage(root).free < Path(source).stat().st_size * 3 + 1024**3:
                raise OSError('insufficient reserve for verified automatic backup')
            backup = target/'rank.db'
            if not backup.exists():
                create_backup(source, backup)
            # Only this managed scratch directory is disposable; source and
            # completed backups are never removed when verification fails.
            scratch = target/'restore-check'
            if scratch.exists():
                if scratch.is_symlink():
                    raise ValueError('invalid scratch directory')
                shutil.rmtree(scratch)
            receipt = verify_restore(backup, scratch)
            write_json(target/'verified.json', receipt)
            shutil.rmtree(scratch)
        return {'phase': 'daily_backup_complete', 'day': day, 'removed': prune(root, keep)}


def main():
    from .config import Settings, PROJECT_ROOT
    os.umask(0o077)
    settings = Settings.from_env()
    if PressureGuard().check():
        print(json.dumps({'phase': 'deferred', 'reason': 'resource_pressure'}), flush=True)
        return 75
    try:
        result = run_backup(settings.database_path, PROJECT_ROOT/'backups'/'auto',
                            datetime.now().astimezone().date().isoformat())
        print(json.dumps(result), flush=True)
        return 0
    except Exception as error:
        print(json.dumps({'phase': 'failed', 'error_type': type(error).__name__}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
