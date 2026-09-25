from __future__ import annotations

import gzip
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.backfill_statistics_snapshots import backfill
from app.database import RankDatabase
from app.exporter import RankExporter
from app.models import RankEntry
from app.statistics_store import StatisticsStore
from tests import test_crawler


def document(snapshot_id):
    return {'schema_version': 1, 'snapshot_id': snapshot_id, 'prefix': '2026',
            'fetched_at': f'2026-09-20T00:{snapshot_id:02d}:00+08:00', 'user_count': 1,
            'historical_roster': {'user_count': 1, 'user_ids': ['202600000001']},
            'users': [{'rank': 1, 'user_id': '202600000001', 'nickname': 'synthetic',
                       'accepted': 1, 'submitted': 2, 'ratio': 50.0, 'level': 'L'}]}


def concurrent_publish(directory, first, barrier):
    store = StatisticsStore(Path(directory))
    barrier.wait(timeout=10)
    for sid in range(first, first + 3):
        store.publish(document(sid))


class StatisticsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = StatisticsStore(self.root)

    def test_interrupted_publication_is_repaired_without_rewriting_gzip(self):
        write = self.store._write
        def interrupted(path, content):
            if path == self.store.manifest_path:
                raise OSError('simulated interruption')
            write(path, content)
        with patch.object(self.store, '_write', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.store.publish(document(1))
        path = self.store.snapshot_path(1)
        before = path.read_bytes(), path.stat().st_mtime_ns
        self.assertTrue(self.store.repair_existing('2026', 1, document(1)['fetched_at']))
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        self.assertEqual([1], list(self.store.index('2026')))

    def test_corrupt_index_is_not_replaced_or_new_snapshot_written(self):
        self.store.manifest_path.write_text('{broken')
        with self.assertRaises(ValueError):
            self.store.publish(document(1))
        self.assertEqual('{broken', self.store.manifest_path.read_text())
        self.assertFalse(self.store.snapshot_path(1).exists())

    def test_immutable_conflict_preserves_existing_history(self):
        self.store.publish(document(1))
        before = self.store.snapshot_path(1).read_bytes()
        changed = document(1)
        changed['users'][0]['nickname'] = 'different'
        with self.assertRaises(ValueError):
            self.store.publish(changed)
        self.assertEqual(before, self.store.snapshot_path(1).read_bytes())
        self.store.publish(document(1))
        self.assertEqual(1, len(self.store.index('2026')))

    def test_corrupt_or_wrong_identity_orphans_are_not_registered(self):
        self.store.snapshot_path(1).parent.mkdir()
        for payload in (b'broken gzip', gzip.compress(json.dumps(document(2)).encode())):
            self.store.snapshot_path(1).write_bytes(payload)
            with self.assertRaises((ValueError, OSError)):
                self.store.repair_existing('2026', 1, document(1)['fetched_at'])
            self.assertFalse(self.store.manifest_path.exists())

    def test_lock_wait_is_bounded_and_released_after_exception(self):
        other = StatisticsStore(self.root)
        other.LOCK_TIMEOUT = 0.01
        with self.store._lock():
            with self.assertRaises(TimeoutError):
                other.publish(document(1))
        other.publish(document(1))

    def test_two_processes_merge_index_without_losing_entries(self):
        context = multiprocessing.get_context('spawn')
        barrier = context.Barrier(2)
        processes = [context.Process(target=concurrent_publish, args=(str(self.root), first, barrier))
                     for first in (1, 4)]
        try:
            for process in processes:
                process.start()
            for process in processes:
                process.join(15)
                self.assertEqual(0, process.exitcode)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join()
        self.assertEqual(list(range(1, 7)), list(self.store.index('2026')))
        self.assertFalse(list(self.root.rglob('*.tmp')))


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = test_crawler.CrawlerTests().settings(self.temp.name)
        self.database = RankDatabase(self.settings.database_path)
        self.database.initialize()
        self.store = StatisticsStore(self.settings.share_dir)
        self.exporter = RankExporter(self.settings.share_dir)
        self.roster = set()
        for sid in range(1, 4):
            entry = RankEntry(1, f'20260000000{sid}', 'synthetic', 1, 2, 50.0, 'L')
            self.database.save_complete_snapshot([entry], '2026', document(sid)['fetched_at'])
            self.roster.add(entry.user_id)
            self.exporter.export_statistics_snapshot([entry], '2026', document(sid)['fetched_at'], sid, self.roster)

    def test_fast_skips_never_load_history_and_are_throttled(self):
        with patch.object(RankDatabase, 'snapshot_entries', side_effect=AssertionError('unexpected history read')), \
             patch.object(RankDatabase, 'initialize', side_effect=AssertionError('unexpected migration')), \
             patch('app.backfill_statistics_snapshots.time.sleep') as sleep:
            self.assertEqual({'published': 0, 'repaired': 0, 'existing': 3}, backfill(self.settings))
            self.assertEqual(3, sleep.call_count)

    def test_repairs_orphan_and_missing_file_preserving_earlier_roster(self):
        manifest = json.loads(self.store.manifest_path.read_text())
        manifest['snapshots'] = manifest['snapshots'][1:]
        self.store.manifest_path.write_text(json.dumps(manifest))
        self.store.snapshot_path(2).unlink()
        original = RankDatabase.snapshot_entries
        calls = []
        def entries(db, sid, prefix):
            calls.append(sid)
            return original(db, sid, prefix)
        with patch.object(RankDatabase, 'snapshot_entries', entries):
            counts = backfill(self.settings, sleep_seconds=0)
        self.assertEqual({'published': 1, 'repaired': 1, 'existing': 1}, counts)
        self.assertEqual([2], calls)
        with gzip.open(self.store.snapshot_path(2), 'rt') as stream:
            self.assertEqual(2, json.load(stream)['historical_roster']['user_count'])
        self.assertEqual([1, 2, 3], list(self.store.index('2026')))

    def test_resume_and_limit_keep_preceding_roster(self):
        self.store.snapshot_path(2).unlink()
        self.store.snapshot_path(3).unlink()
        counts = backfill(self.settings, start_id=2, limit=1, sleep_seconds=0)
        self.assertEqual(1, counts['published'])
        self.assertFalse(self.store.snapshot_path(3).exists())
        with gzip.open(self.store.snapshot_path(2), 'rt') as stream:
            self.assertEqual(2, json.load(stream)['historical_roster']['user_count'])

    def test_readonly_connection_cannot_mutate_database(self):
        database = RankDatabase(self.settings.database_path, read_only=True)
        with database._connect() as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('DELETE FROM snapshots')
        self.assertEqual(3, len(self.database.snapshot_metadata('2026')))

    def test_explicit_verification_detects_corrupt_indexed_file(self):
        self.store.snapshot_path(1).write_bytes(b'broken gzip')
        with self.assertRaises(OSError):
            backfill(self.settings, sleep_seconds=0, verify_existing=True)

    def test_rejects_nonfinite_throttle(self):
        for value in (float('nan'), float('inf'), -1):
            with self.assertRaises(ValueError):
                backfill(self.settings, sleep_seconds=value)
