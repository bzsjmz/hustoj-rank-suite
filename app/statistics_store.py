"""Recoverable, single-prefix history publication with cooperating process locks."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import gzip
import json
import os
from pathlib import Path
import tempfile
import time

from .models import RankEntry, validate_rank_entry


class StatisticsStore:
    LOCK_TIMEOUT = 5.0

    def __init__(self, share_dir: Path):
        self.share_dir = share_dir
        self.manifest_path = share_dir / 'statistics-snapshots.json'

    def snapshot_path(self, snapshot_id: int) -> Path:
        if type(snapshot_id) is not int or snapshot_id <= 0:
            raise ValueError('invalid snapshot id')
        return self.share_dir / 'statistics-snapshots' / f'snapshot-{snapshot_id}.json.gz'

    @contextmanager
    def _lock(self):
        self.share_dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.share_dir / '.statistics-snapshots.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + self.LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('statistics publication lock timed out') from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(descriptor)  # Releases the lock even on exceptions/process exit.

    @staticmethod
    def _write(path: Path, content: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(content)
                os.fchmod(stream.fileno(), 0o644)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _metadata(document: dict) -> dict:
        return {key: document[key] for key in ('snapshot_id', 'fetched_at', 'user_count')}

    @staticmethod
    def _validate_metadata(item) -> None:
        if (not isinstance(item, dict)
                or type(item.get('snapshot_id')) is not int or item['snapshot_id'] <= 0
                or not isinstance(item.get('fetched_at'), str) or not item['fetched_at']
                or type(item.get('user_count')) is not int or item['user_count'] <= 0):
            raise ValueError('invalid statistics index entry')

    def index(self, prefix: str) -> dict[int, dict]:
        try:
            document = json.loads(self.manifest_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        # Corrupt/unreadable indexes must never silently become empty indexes.
        if (not isinstance(document, dict) or document.get('schema_version') != 1
                or document.get('prefix') != prefix or not isinstance(document.get('snapshots'), list)):
            raise ValueError('invalid statistics index schema or prefix')
        result = {}
        for item in document['snapshots']:
            self._validate_metadata(item)
            key = item['snapshot_id']
            if key in result:
                raise ValueError('duplicate snapshot in statistics index')
            result[key] = item
        return result

    def _validate_document(self, document, prefix: str, snapshot_id: int, fetched_at: str) -> None:
        if not isinstance(document, dict):
            raise ValueError('invalid statistics snapshot')
        self._validate_metadata(document)
        if (document.get('schema_version') != 1 or document.get('prefix') != prefix
                or document['snapshot_id'] != snapshot_id or document['fetched_at'] != fetched_at):
            raise ValueError('statistics snapshot identity mismatch')
        users, roster = document.get('users'), document.get('historical_roster')
        if not isinstance(users, list) or len(users) != document['user_count'] or not isinstance(roster, dict):
            raise ValueError('invalid statistics snapshot counts')
        ids = roster.get('user_ids')
        if (not isinstance(ids, list) or not ids or any(not isinstance(uid, str) or not uid for uid in ids)
                or len(set(ids)) != len(ids) or roster.get('user_count') != len(ids)):
            raise ValueError('invalid statistics roster')
        roster_ids = set(ids)
        seen = set()
        for row in users:
            if not isinstance(row, dict):
                raise ValueError('invalid statistics row')
            try:
                entry = RankEntry(**row)
                validate_rank_entry(entry)
            except (TypeError, ValueError):
                raise ValueError('invalid statistics row') from None
            if entry.user_id not in roster_ids or entry.user_id in seen:
                raise ValueError('invalid statistics membership')
            seen.add(entry.user_id)

    def _register(self, prefix: str, by_id: dict, metadata: dict) -> None:
        existing = by_id.get(metadata['snapshot_id'])
        if existing is not None:
            if existing != metadata:
                raise ValueError('conflicting statistics index metadata')
            return
        by_id[metadata['snapshot_id']] = metadata
        document = {'schema_version': 1, 'prefix': prefix,
                    'snapshots': [by_id[key] for key in sorted(by_id)]}
        self._write(self.manifest_path, (json.dumps(document, ensure_ascii=False, separators=(',', ':')) + '\n').encode())

    def repair_existing(self, prefix: str, snapshot_id: int, fetched_at: str) -> bool:
        """Register a validated orphan; no database rows need to be loaded."""
        with self._lock():
            by_id = self.index(prefix)
            path = self.snapshot_path(snapshot_id)
            if not path.is_file():
                return False
            with gzip.open(path, 'rt', encoding='utf-8') as stream:
                document = json.load(stream)
            self._validate_document(document, prefix, snapshot_id, fetched_at)
            self._register(prefix, by_id, self._metadata(document))
            return True

    def publish(self, document: dict) -> None:
        prefix, snapshot_id, fetched_at = document['prefix'], document['snapshot_id'], document['fetched_at']
        self._validate_document(document, prefix, snapshot_id, fetched_at)
        payload = gzip.compress(json.dumps(document, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode(), compresslevel=6, mtime=0)
        with self._lock():
            by_id = self.index(prefix)
            metadata = self._metadata(document)
            if snapshot_id in by_id and by_id[snapshot_id] != metadata:
                raise ValueError('conflicting statistics index metadata')
            path = self.snapshot_path(snapshot_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.chmod(0o755)
            if path.exists():
                with gzip.open(path, 'rt', encoding='utf-8') as stream:
                    previous = json.load(stream)
                if previous != document:
                    raise ValueError('refusing to overwrite immutable statistics snapshot')
            else:
                self._write(path, payload)
            # A crash here leaves a valid orphan. Retrying or repair_existing
            # registers it without rewriting history. Index is always published last.
            self._register(prefix, by_id, metadata)
