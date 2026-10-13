import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.daily_records import DailyRecordWorker, export_daily_records, update_daily_records
from app.database import RankDatabase
from app.exporter import RankExporter
from app.models import RankEntry
from app.student_ids import StudentIdLayout


class DailyRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = RankDatabase(self.root / 'rank.db')
        self.db.initialize()
        self.layout = StudentIdLayout.conventional('2026')
        self.sid = 0
        self.stamp = ''

    def save(self, stamp, values):
        entries = [RankEntry(i, uid, f'nick-{uid}', ac, ac + 5, 80, '')
                   for i, (uid, ac) in enumerate(values.items(), 1)]
        self.sid = self.db.save_complete_snapshot(entries, '2026', stamp)
        self.stamp = stamp

    def export(self, excluded=frozenset(), *, update=True):
        if update:
            update_daily_records(self.db, "2026", self.stamp, self.sid, throttle_seconds=0)
        return export_daily_records(self.db, RankExporter(self.root), '2026',
                                    self.stamp, self.sid, excluded, self.layout)

    def test_backfill_ties_repeated_user_and_today_excluded(self):
        a, b = '202600000001', '202600000002'
        for stamp, ac in [('2026-10-10T23:55:00+08:00', 10),
                          ('2026-10-11T23:55:00+08:00', 30),
                          ('2026-10-12T23:55:00+08:00', 50),
                          ('2026-10-13T08:00:00+08:00', 100)]:
            self.save(stamp, {a: ac, b: ac})
        result = self.export()
        self.assertEqual(4, result['record_count'])
        self.assertEqual([1, 1, 1, 1], [r['rank'] for r in result['users']])
        self.assertEqual({'2026-10-11', '2026-10-12'}, {r['date'] for r in result['users']})
        self.assertEqual(2, sum(r['user_id'] == a for r in result['users']))
        self.assertEqual(result, self.export())
        self.assertEqual(2, self.export({a})['record_count'])
        # Cached derivation stays intact when current exclusion policy changes.
        self.assertEqual(4, self.export()['record_count'])
        self.assertEqual(result, json.loads((self.root / 'historical-daily.json').read_text()))

    def test_missing_boundary_never_counts_lifetime_ac(self):
        a = '202600000001'
        self.save('2026-10-11T08:00:00+08:00', {a: 200})
        self.save('2026-10-12T08:00:00+08:00', {a: 250})
        self.assertEqual([], self.export()['users'])

    def test_missing_account_new_account_and_observed_reset_excluded(self):
        a, b, c, d = [f'20260000000{i}' for i in range(1, 5)]
        self.save('2026-10-10T23:55:00+08:00', {a: 10, b: 10, c: 10})
        self.save('2026-10-11T10:00:00+08:00', {a: 20, c: 5, d: 200})
        self.save('2026-10-11T23:55:00+08:00', {a: 30, b: 30, c: 30, d: 250})
        self.save('2026-10-12T01:00:00+08:00', {a: 40})
        result = self.export()
        self.assertEqual([(a, 20)], [(r['user_id'], r['accepted_change']) for r in result['users']])

    def test_utc_timestamps_and_nonstudent_filter(self):
        a = '202600000001'
        self.save('2026-10-10T15:55:00+00:00', {a: 10, 'service': 10})
        self.save('2026-10-11T15:55:00+00:00', {a: 30, 'service': 50})
        self.save('2026-10-11T16:05:00+00:00', {a: 40, 'service': 60})
        result = self.export()
        self.assertEqual(1, result['record_count'])
        self.assertEqual('2026-10-11', result['users'][0]['date'])
        self.assertEqual(20, result['users'][0]['accepted_change'])

    def test_new_closed_day_and_gap_have_no_duplicate_rows(self):
        a = '202600000001'
        self.save('2026-10-10T23:55:00+08:00', {a: 10})
        self.save('2026-10-11T23:55:00+08:00', {a: 30})
        self.save('2026-10-12T00:05:00+08:00', {a: 35})
        self.assertEqual(1, self.export()['record_count'])
        self.save('2026-10-12T23:55:00+08:00', {a: 60})
        self.save('2026-10-13T00:05:00+08:00', {a: 65})
        self.assertEqual([30, 20], [r['accepted_change'] for r in self.export()['users']])
        self.save('2026-10-15T00:05:00+08:00', {a: 100})
        self.assertEqual(2, self.export()['record_count'])

    def test_cache_does_not_read_historical_rank_rows_again(self):
        a = '202600000001'
        self.save('2026-10-10T23:55:00+08:00', {a: 10})
        self.save('2026-10-11T23:55:00+08:00', {a: 30})
        self.save('2026-10-12T00:05:00+08:00', {a: 35})
        self.export()
        statements = []
        original = self.db._connect
        from contextlib import contextmanager
        @contextmanager
        def traced():
            with original() as connection:
                connection.set_trace_callback(statements.append)
                yield connection
        with patch.object(self.db, '_connect', traced):
            self.export()
        self.assertFalse(any('FROM rank_snapshots' in sql for sql in statements))


    def test_publication_reads_cache_without_history_scan_or_schema_writes(self):
        self.save('2026-10-12T00:05:00+08:00', {'202600000001': 35})
        with patch.object(self.db, 'snapshot_metadata', side_effect=AssertionError('must not scan')):
            result = self.export(update=False)
        self.assertEqual([], result['users'])
        self.assertEqual('updating', result['history_status'])
        with self.db._connect() as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name = 'daily_ac_records'").fetchone())

    def test_interrupted_backfill_commits_days_and_resumes(self):
        a = '202600000001'
        for stamp, ac in [('2026-10-10T23:55:00+08:00', 10),
                          ('2026-10-11T23:55:00+08:00', 30),
                          ('2026-10-12T23:55:00+08:00', 60),
                          ('2026-10-13T00:05:00+08:00', 65)]:
            self.save(stamp, {a: ac})
        pauses = iter([False, False, True])
        self.assertFalse(update_daily_records(self.db, '2026', self.stamp, self.sid,
                                             pause=lambda: next(pauses), throttle_seconds=0))
        partial = self.export(update=False)
        self.assertEqual(1, partial['record_count'])
        self.assertEqual('updating', partial['history_status'])
        completed = self.export()
        self.assertEqual([30, 20], [row['accepted_change'] for row in completed['users']])
        self.assertEqual('ready', completed['history_status'])
        self.assertEqual(completed, self.export())

    def test_mixed_timezone_endpoints_export_as_aware(self):
        a = '202600000001'
        self.save('2026-10-10T23:55:00', {a: 10})
        self.save('2026-10-11T23:55:00+08:00', {a: 30})
        self.save('2026-10-12T00:05:00+08:00', {a: 35})
        row = self.export()['users'][0]
        self.assertEqual('2026-10-10T23:55:00+08:00', row['start_at'])
        self.assertEqual('2026-10-11T23:55:00+08:00', row['end_at'])
        # Previously persisted cache rows are normalized on export too.
        with self.db._connect() as connection:
            connection.execute("UPDATE daily_ac_records SET start_at = '2026-10-10T23:55:00'")
        self.assertEqual(row, self.export(update=False)['users'][0])

    def test_worker_runs_separately_and_does_not_overlap(self):
        import logging
        from unittest.mock import Mock
        worker = DailyRecordWorker(self.db.path, logging.getLogger('fixture'))
        child = Mock()
        child.poll.return_value = None
        with patch('app.daily_records.subprocess.Popen', return_value=child) as launch:
            worker.start('2026', '2026-10-13T00:05:00+08:00', 3)
            worker.start('2026', '2026-10-13T00:10:00+08:00', 4)
            self.assertEqual(1, launch.call_count)
            worker.close()
            child.terminate.assert_called_once()
            child.wait.assert_called_once_with(timeout=5)

    def test_worker_launch_failure_is_nonfatal(self):
        import logging
        worker = DailyRecordWorker(self.db.path, logging.getLogger('fixture'))
        with patch('app.daily_records.subprocess.Popen', side_effect=OSError('fixture')):
            worker.start('2026', '2026-10-13T00:05:00+08:00', 3)
        self.assertIsNone(worker.process)
        worker.close()


    def test_real_background_worker_builds_cache_in_isolated_database(self):
        a = '202600000001'
        self.save('2026-10-10T23:55:00', {a: 10})
        self.save('2026-10-11T23:55:00+08:00', {a: 30})
        self.save('2026-10-12T00:05:00+08:00', {a: 35})
        # No host pressure dependency: invoke the worker CLI with a mocked guard.
        import subprocess
        import sys
        command = [sys.executable, '-B', '-c',
                   'from unittest.mock import patch; from app.daily_records import main; '
                   'patch("app.resource_guard.PressureGuard.check", return_value=False).start(); '
                   'raise SystemExit(main())',
                   '--database', str(self.db.path), '--prefix', '2026',
                   '--fetched-at', self.stamp, '--snapshot-id', str(self.sid)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        payload = self.export(update=False)
        self.assertEqual('ready', payload['history_status'])
        self.assertEqual(20, payload['users'][0]['accepted_change'])
        with self.db._connect() as connection:
            self.assertEqual(3, connection.execute('SELECT count(*) FROM snapshots').fetchone()[0])
            self.assertEqual(3, connection.execute('SELECT count(*) FROM rank_snapshots').fetchone()[0])

    def test_cli_lock_prevents_concurrent_backfill(self):
        import fcntl
        import subprocess
        import sys
        self.save('2026-10-12T00:05:00+08:00', {'202600000001': 35})
        lock_path = self.db.path.with_suffix('.db.daily-records.lock')
        with lock_path.open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run([sys.executable, '-B', '-m', 'app.daily_records',
                '--database', str(self.db.path), '--prefix', '2026',
                '--fetched-at', self.stamp, '--snapshot-id', str(self.sid)],
                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        with self.db._connect() as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name = 'daily_ac_records'").fetchone())
