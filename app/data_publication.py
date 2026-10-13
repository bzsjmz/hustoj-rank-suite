"""Immutable current-data generations; SQLite/history/images remain separate."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import uuid

REQUIRED = ('latest.json', 'latest.csv', 'lookup.json', 'academic-labels.json',
            'computer-college.json', 'software-college.json', 'class-intensity.json',
            'major-intensity.json', 'weekly.json', 'monthly.json', 'historical-daily.json',
            'class-images/manifest.json', 'major-images/manifest.json')


def current_data_root(share):
    share = Path(share)
    pointer = share/'current-data'
    if not pointer.exists() and not pointer.is_symlink():
        return share
    resolved = pointer.resolve(strict=True)
    if resolved.parent != (share/'.data-versions').resolve() or not resolved.is_dir():
        raise ValueError('invalid data generation pointer')
    return resolved


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class DataPublication:
    def __init__(self, share, snapshot_id, fetched_at):
        self.share = Path(share)
        self.snapshot_id = snapshot_id
        self.fetched_at = fetched_at
        versions = self.share/'.data-versions'
        versions.mkdir(exist_ok=True, mode=0o755)
        self.path = Path(tempfile.mkdtemp(prefix='.pending-', dir=versions))
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if not self.committed:
            shutil.rmtree(self.path, ignore_errors=True)

    def validate(self):
        for name in REQUIRED:
            if not (self.path/name).is_file():
                raise ValueError('incomplete data generation: ' + name)
        for path in self.path.rglob('*.json'):
            doc = json.loads(path.read_text())
            if path.name == 'academic-labels.json':
                continue
            if (doc.get('snapshot_id'), doc.get('fetched_at')) != (self.snapshot_id, self.fetched_at):
                raise ValueError('data generation identity mismatch')
            for key in ('users', 'changes'):
                if key in doc and (not isinstance(doc[key], list) or doc.get('user_count') != len(doc[key])):
                    raise ValueError('data generation count mismatch')
        for kind, key in (('class', 'classes'), ('major', 'majors')):
            doc = json.loads((self.path/f'{kind}-images/manifest.json').read_text())
            for row in doc[key]:
                entity = row[f'{kind}_id']
                if not isinstance(entity, str) or not entity.isascii() or not entity.isalnum():
                    raise ValueError('invalid registry identifier')
                data = json.loads((self.path/f'{kind}-ranklists/{entity}.json').read_text())
                if data['user_count'] != row['user_count']:
                    raise ValueError('registry count mismatch')

    def commit(self):
        self.validate()
        files = sorted(p.relative_to(self.path).as_posix() for p in self.path.rglob('*') if p.is_file())
        receipt = {'schema_version': 1, 'snapshot_id': self.snapshot_id,
                   'fetched_at': self.fetched_at, 'files': files, 'published_at': time.time()}
        (self.path/'publication.json').write_text(json.dumps(receipt) + '\n')
        for path in self.path.rglob('*'):
            if path.is_file():
                path.chmod(0o644)
                with path.open('rb') as f:
                    os.fsync(f.fileno())
        for path in sorted((p for p in self.path.rglob('*') if p.is_dir()), reverse=True):
            path.chmod(0o755)
            fsync_dir(path)
        self.path.chmod(0o755)
        fsync_dir(self.path)
        final = self.path.parent/f'{self.snapshot_id}-{uuid.uuid4().hex}'
        self.path.rename(final)
        self.path = final
        fsync_dir(final.parent)
        pointer = self.share/('.current-' + uuid.uuid4().hex)
        try:
            pointer.symlink_to(final.relative_to(self.share))
            pointer.replace(self.share/'current-data')
            self.committed = True
            fsync_dir(self.share)
        finally:
            pointer.unlink(missing_ok=True)
        return receipt

    def refresh_legacy_aliases(self):
        # Compatibility only. Updated consumers resolve current-data once;
        # legacy multi-file readers do not get a pinned read transaction.
        doc = json.loads((self.path/'publication.json').read_text())
        for name in doc['files']:
            target = self.share/name
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.parent/('.alias-' + uuid.uuid4().hex)
            try:
                temporary.symlink_to(os.path.relpath(self.share/'current-data'/name, target.parent))
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)

    def prune(self, keep=3, minimum_age=3600):
        # Only complete generations managed by this module; never SQLite history.
        current = current_data_root(self.share)
        candidates = []
        for path in self.path.parent.iterdir():
            if path.is_symlink() or not path.is_dir() or path.name.startswith('.'):
                continue
            try:
                doc = json.loads((path/'publication.json').read_text())
                stamp = float(doc['published_at'])
                if doc.get('schema_version') != 1:
                    continue
                candidates.append((stamp, path))
            except (OSError, ValueError, KeyError):
                continue
        for stamp, path in sorted(candidates, reverse=True)[keep:]:
            if path != current and time.time()-stamp > minimum_age:
                shutil.rmtree(path)
