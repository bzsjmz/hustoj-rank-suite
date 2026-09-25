import json
import unittest
from pathlib import Path
from app.scheduled_backup import run_backup, prune, MARKER
from tests import test_database_backup


class ScheduledBackupTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_database_backup.DatabaseBackupTests();self.fixture.setUp()
        self.root=self.fixture.root/'auto'
    def tearDown(self):self.fixture.tearDown()
    def test_verified_job_is_idempotent_and_preserves_manual_files(self):
        result=run_backup(self.fixture.source,self.root,'2026-01-01')
        self.assertEqual('daily_backup_complete',result['phase'])
        target=self.root/'2026-01-01';stamp=(target/'rank.db').stat().st_mtime_ns
        run_backup(self.fixture.source,self.root,'2026-01-01')
        self.assertEqual(stamp,(target/'rank.db').stat().st_mtime_ns)
        self.assertFalse((target/'restore-check').exists())
    def test_only_owned_verified_backups_are_pruned(self):
        for day in ['2026-01-01','2026-01-02','2026-01-03']:
            run_backup(self.fixture.source,self.root,day,keep=2)
        manual=self.root/'2025-01-01';manual.mkdir();(manual/'rank.db').write_text('manual')
        incomplete=self.root/'2025-02-01';incomplete.mkdir();(incomplete/'managed.json').write_text(json.dumps(MARKER))
        self.assertEqual([],prune(self.root,keep=2))
        self.assertTrue(manual.exists());self.assertTrue(incomplete.exists())
        self.assertFalse((self.root/'2026-01-01').exists())
    def test_unowned_destination_is_rejected(self):
        self.root.mkdir(mode=0o700);(self.root/'2026-01-01').mkdir()
        with self.assertRaises(ValueError):run_backup(self.fixture.source,self.root,'2026-01-01')
