from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.database import RankDatabase
from app.models import RankEntry


def entry(user_id: str, rank: int = 1) -> RankEntry:
    return RankEntry(rank, user_id, "nick", 10, 12, 83.333, "level")


class DatabaseTests(unittest.TestCase):
    def test_snapshot_and_current_rank_are_written_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rank.db"
            database = RankDatabase(path)
            database.initialize()
            snapshot_id = database.save_complete_snapshot(
                [entry("202600000001"), entry("202600000002", 2)],
                "2026",
                "2026-08-23T10:00:00+08:00",
            )
            self.assertEqual(1, snapshot_id)
            self.assertEqual(2, database.current_count("2026"))
            with sqlite3.connect(path) as connection:
                count = connection.execute("SELECT COUNT(*) FROM rank_snapshots").fetchone()[0]
            self.assertEqual(2, count)

    def test_empty_or_duplicate_result_never_replaces_current_rank(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = RankDatabase(Path(directory) / "rank.db")
            database.initialize()
            database.save_complete_snapshot(
                [entry("202600000001")], "2026", "2026-08-23T10:00:00+08:00"
            )
            with self.assertRaises(ValueError):
                database.save_complete_snapshot([], "2026", "later")
            with self.assertRaises(ValueError):
                database.save_complete_snapshot(
                    [entry("202600000002"), entry("202600000002", 2)],
                    "2026",
                    "later",
                )
            self.assertEqual(1, database.current_count("2026"))

    def test_daily_baseline_uses_first_snapshot_and_normalizes_exclusions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = RankDatabase(Path(directory) / "rank.db")
            database.initialize()
            first_id = database.save_complete_snapshot(
                [
                    entry("excluded", 1),
                    entry("alice", 2),
                    entry("bob", 3),
                ],
                "2026",
                "2026-08-23T08:00:00+08:00",
            )
            current_id = database.save_complete_snapshot(
                [entry("bob", 1), entry("alice", 2)],
                "2026",
                "2026-08-23T10:00:00+08:00",
            )

            baseline = database.daily_baseline(
                "2026",
                "2026-08-23T10:00:00+08:00",
                current_id,
                frozenset({"excluded"}),
            )

            self.assertEqual(first_id, baseline.snapshot_id)
            self.assertEqual(
                [("alice", 1), ("bob", 2)],
                [(item.user_id, item.rank) for item in baseline.entries],
            )

    def test_reads_complete_roster_from_historical_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = RankDatabase(Path(directory) / "rank.db")
            database.initialize()
            snapshot_id = database.save_complete_snapshot(
                [entry("202600000001"), entry("202600000002", 2)],
                "2026",
                "2026-08-23T08:00:00+08:00",
            )

            self.assertEqual(
                frozenset({"202600000001", "202600000002"}),
                database.snapshot_user_ids(snapshot_id, "2026"),
            )
            with self.assertRaises(ValueError):
                database.snapshot_user_ids(snapshot_id, "other")


    def test_historical_roster_unions_all_complete_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = RankDatabase(Path(directory) / "rank.db")
            database.initialize()
            database.save_complete_snapshot(
                [entry("202600000001"), entry("202600000002", 2)],
                "2026",
                "2026-08-23T08:00:00+08:00",
            )
            database.save_complete_snapshot(
                [entry("202600000002"), entry("202600000003", 2)],
                "2026",
                "2026-08-23T10:00:00+08:00",
            )
            database.save_complete_snapshot(
                [entry("20260001")], "2026", "2026-08-23T12:00:00+08:00"
            )

            self.assertEqual(
                frozenset({"202600000001", "202600000002", "202600000003"}),
                database.historical_roster_user_ids("2026"),
            )
            self.assertEqual(
                frozenset({"202600000001", "202600000003"}),
                database.historical_roster_user_ids("2026", {"202600000002"}),
            )


    def test_reads_entries_and_roster_at_historical_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = RankDatabase(Path(directory) / "rank.db")
            database.initialize()
            first = database.save_complete_snapshot(
                [entry("202600000001"), entry("202600000002", 2)],
                "2026", "2026-08-23T08:00:00+08:00",
            )
            second = database.save_complete_snapshot(
                [entry("202600000002"), entry("202600000003", 2)],
                "2026", "2026-08-23T10:00:00+08:00",
            )
            self.assertEqual(["202600000002", "202600000003"], [
                item.user_id for item in database.snapshot_entries(second, "2026")
            ])
            self.assertEqual(
                frozenset({"202600000001", "202600000002", "202600000003"}),
                database.historical_roster_user_ids_at_snapshot(second, "2026"),
            )
            self.assertEqual(
                ((first, "2026-08-23T08:00:00+08:00", 2), (second, "2026-08-23T10:00:00+08:00", 2)),
                database.snapshot_metadata("2026"),
            )

    def test_reads_latest_historical_entry_for_missing_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = RankDatabase(Path(directory) / "rank.db")
            database.initialize()
            database.save_complete_snapshot(
                [RankEntry(1, "202600000001", "A", 4, 5, 80.0, "L1")],
                "2026",
                "2026-08-23T08:00:00+08:00",
            )
            database.save_complete_snapshot(
                [RankEntry(1, "202600000002", "B", 6, 7, 85.7, "L2")],
                "2026",
                "2026-08-23T10:00:00+08:00",
            )

            latest = database.latest_entries_for_user_ids("2026", {"202600000001"})
            self.assertEqual("2026-08-23T08:00:00+08:00", latest["202600000001"].fetched_at)
            self.assertEqual(4, latest["202600000001"].entry.accepted)


if __name__ == "__main__":
    unittest.main()
