from __future__ import annotations

import json
import gzip
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from app import main
from app.database import RankDatabase
from app.models import RankEntry
from tests import test_crawler


def sample(user_id="202600000001", rank=1):
    return RankEntry(rank, user_id, "synthetic", 1, 2, 50.0, "level")


class CollectionIntegrityTests(unittest.TestCase):
    def run_cycle(self, settings, entries):
        event = threading.Event()
        crawler = Mock()
        crawler.scrape_all.return_value = entries
        submission_service = Mock()
        submission_service.wait.side_effect = lambda _timeout: event.set() or False
        guard = Mock(paused=False)
        guard.check.return_value = False
        with patch.object(main.Settings, "from_env", return_value=settings), \
             patch.object(main, "RankCrawler", return_value=crawler), \
             patch.object(main, "SubmissionService", return_value=submission_service), \
             patch.object(main, "PressureGuard", return_value=guard), \
             patch.object(main, "write_status"), \
             patch.object(main, "configure_logging", return_value=Mock()), \
             patch.object(main.signal, "signal"), \
             patch.object(main.threading, "Event", return_value=event):
            self.assertEqual(0, main.run())

    def settings(self, directory):
        return replace(test_crawler.CrawlerTests().settings(directory),
                       lookup_prefixes=("2026",), college_split_snapshot_id=1)

    def test_source_accounts_preserved_but_student_export_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            self.run_cycle(settings, [sample("2026special"), sample(rank=2)])
            with sqlite3.connect(settings.database_path) as db:
                self.assertEqual(2, db.execute("SELECT COUNT(*) FROM rank_snapshots").fetchone()[0])
            result = json.loads((settings.share_dir / "latest.json").read_text())
            self.assertEqual(1, result["user_count"])
            self.assertEqual(1, result["users"][0]["rank"])
            self.assertEqual(1, result["historical_roster"]["user_count"])
            self.assertEqual(1.0, result["coverage_rate"])
            self.assertEqual(0, result["missing_user_count"])
            daily = json.loads((settings.share_dir / "daily.json").read_text())
            self.assertEqual(1, daily["changes"][0]["baseline_rank"])
            self.assertEqual(0, daily["changes"][0]["rank_change"])
            with gzip.open(settings.share_dir / "statistics-snapshots/snapshot-1.json.gz", "rt") as stream:
                history = json.load(stream)
            self.assertEqual(2, history["users"][0]["rank"])

    def test_no_students_preserves_previous_database_and_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            self.run_cycle(settings, [sample()])
            previous = (settings.share_dir / "latest.json").read_bytes()
            self.run_cycle(settings, [sample("2026special")])
            self.assertEqual(previous, (settings.share_dir / "latest.json").read_bytes())
            with sqlite3.connect(settings.database_path) as db:
                self.assertEqual(1, db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])
                self.assertEqual(1, db.execute("SELECT COUNT(*) FROM current_rank").fetchone()[0])

    def test_invalid_programmatic_entries_do_not_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            database = RankDatabase(Path(directory) / "rank.db")
            database.initialize()
            database.save_complete_snapshot([sample()], "2026", "2026-09-20T00:00:00+08:00")
            for field, value in (("rank", True), ("accepted", -1), ("submitted", "2"),
                                 ("ratio", float("nan")), ("ratio", float("inf")),
                                 ("ratio", 101), ("nickname", None), ("user_id", 1)):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    database.save_complete_snapshot([replace(sample(), **{field: value})], "2026", "later")
            with sqlite3.connect(database.path) as db:
                self.assertEqual(1, db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0])
