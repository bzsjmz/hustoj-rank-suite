import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.database_backup import create_backup, verify_restore


class DatabaseBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.db'
        self.db = sqlite3.connect(self.source)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE snapshots(id INTEGER PRIMARY KEY, fetched_at TEXT, user_count INTEGER);
            INSERT INTO snapshots VALUES(1,'2026-01-01',1);
            CREATE TABLE rank_snapshots(snapshot_id INTEGER, payload BLOB);
            INSERT INTO rank_snapshots VALUES(1,zeroblob(1048576));
            CREATE TABLE current_rank(id INTEGER);
            INSERT INTO current_rank VALUES(1);
            CREATE TABLE student_roster(id INTEGER);
            INSERT INTO student_roster VALUES(1);
        ''')
        self.output = self.root / 'private' / 'backup.db'

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_backup_restore_and_write_rollback(self):
        result = create_backup(self.source, self.output, pause=0)
        self.assertEqual(1, result['snapshot_count'])
        self.assertEqual(0o600, self.output.stat().st_mode & 0o777)
        self.assertFalse(Path(str(self.output)+'-wal').exists())
        result = verify_restore(self.output, self.root/'restore')
        self.assertEqual('ok', result['quick_check'])
        self.assertEqual('ok', result['write_rollback'])
        self.assertEqual(1, self.db.execute('select count(*) from snapshots').fetchone()[0])

    def test_concurrent_commit_does_not_change_pinned_snapshot(self):
        changed = False
        def write_during_copy(_):
            nonlocal changed
            if not changed:
                self.db.execute("INSERT INTO snapshots VALUES(2,'later',1)")
                self.db.commit()
                changed = True
        with patch('app.database_backup.time.sleep', side_effect=write_during_copy):
            result = create_backup(self.source, self.output)
        self.assertTrue(changed)
        self.assertEqual(1, result['snapshot_count'])
        self.assertEqual(2, self.db.execute('select count(*) from snapshots').fetchone()[0])

    def test_existing_output_and_restore_directory_are_never_overwritten(self):
        create_backup(self.source, self.output, pause=0)
        original = self.output.read_bytes()
        with self.assertRaises(FileExistsError):
            create_backup(self.source, self.output)
        self.assertEqual(original, self.output.read_bytes())
        with self.assertRaises(FileExistsError):
            verify_restore(self.output, self.root)

    def test_timeout_does_not_publish_partial_backup(self):
        with self.assertRaises(TimeoutError):
            create_backup(self.source, self.output, max_seconds=-1, pause=0)
        self.assertFalse(self.output.exists())
        self.assertEqual([], list(self.output.parent.glob('.backup-*')))

    def test_non_wal_source_is_rejected_without_changing_mode(self):
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.db.execute('PRAGMA journal_mode=DELETE')
        with self.assertRaises(ValueError):
            create_backup(self.source, self.output)
        self.assertEqual('delete', self.db.execute('PRAGMA journal_mode').fetchone()[0])

    def test_corrupt_backup_fails_restore_verification(self):
        self.output.parent.mkdir(mode=0o700)
        self.output.write_bytes(b'not a database')
        with self.assertRaises(sqlite3.Error):
            verify_restore(self.output, self.root/'bad-restore')
        self.assertFalse((self.root/'bad-restore'/'verification.json').exists())
