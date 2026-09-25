from __future__ import annotations

import unittest
from unittest.mock import patch
from app.college import MAJOR_DISPLAY_NAMES

from app.college import (
    build_class_intensity,
    build_major_intensity,
    class_display_name,
    major_display_name,
    merge_class_history,
    split_and_rerank_classes,
    split_and_rerank_colleges,
    split_and_rerank_majors,
)
from app.models import HistoricalRankEntry, RankEntry
from app.ranking import exclude_and_rerank, select_prefix_exclude_and_rerank


class RankingTests(unittest.TestCase):
    def setUp(self):
        labels = patch.dict(MAJOR_DISPLAY_NAMES, {"0723": "示例甲", "0703": "示例乙"}, clear=True)
        labels.start()
        self.addCleanup(labels.stop)

    def test_excludes_configured_users_and_makes_ranks_continuous(self) -> None:
        entries = [
            RankEntry(1, "keep-a", "A", 3, 4, 75.0, "L1"),
            RankEntry(2, "20260001", "xiaoxiao", 3, 4, 75.0, "L1"),
            RankEntry(3, "keep-b", "B", 2, 4, 50.0, "L1"),
            RankEntry(4, "202617229", "后起之秀", 2, 4, 50.0, "L1"),
        ]

        result = exclude_and_rerank(
            entries, frozenset({"20260001", "202617229"})
        )

        self.assertEqual(["keep-a", "keep-b"], [item.user_id for item in result])
        self.assertEqual([1, 2], [item.rank for item in result])

    def test_selects_prefix_from_school_wide_scoped_rank(self) -> None:
        entries = [
            RankEntry(1, "other", "Other", 10, 10, 100.0, "L1"),
            RankEntry(2, "20260002", "B", 8, 10, 80.0, "L1"),
            RankEntry(3, "20260001", "Old", 7, 10, 70.0, "L1"),
            RankEntry(4, "20260003", "C", 6, 10, 60.0, "L1"),
        ]

        result = select_prefix_exclude_and_rerank(
            entries, "2026", frozenset({"20260001"})
        )

        self.assertEqual(["20260002", "20260003"], [item.user_id for item in result])
        self.assertEqual([1, 2], [item.rank for item in result])

    def test_college_split_restarts_rank_with_frozen_roster(self) -> None:
        entries = [
            RankEntry(1, "soft-top", "S", 10, 10, 100.0, "L1"),
            RankEntry(2, "computer-second", "C", 9, 10, 90.0, "L1"),
            RankEntry(3, "soft-third", "S2", 8, 10, 80.0, "L1"),
            RankEntry(4, "computer-fourth", "C2", 7, 10, 70.0, "L1"),
        ]

        computer, software = split_and_rerank_colleges(
            entries, {"computer-second", "computer-fourth"}
        )

        self.assertEqual(
            [("computer-second", 1), ("computer-fourth", 2)],
            [(entry.user_id, entry.rank) for entry in computer],
        )
        self.assertEqual(
            [("soft-top", 1), ("soft-third", 2)],
            [(entry.user_id, entry.rank) for entry in software],
        )

    def test_class_split_uses_first_ten_digits_and_one_local_ranking(self) -> None:
        entries = [
            RankEntry(1, "202607230612", "A", 10, 10, 100.0, "L1"),
            RankEntry(2, "202607240611", "B", 9, 10, 90.0, "L1"),
            RankEntry(3, "202607230613", "C", 8, 10, 80.0, "L1"),
        ]

        classes = split_and_rerank_classes(entries, "2026")

        self.assertEqual(["2026072306", "2026072406"], list(classes))
        self.assertEqual(
            [("202607230612", 1), ("202607230613", 2)],
            [(entry.user_id, entry.rank) for entry in classes["2026072306"]],
        )

    def test_class_history_is_appended_without_changing_live_ranks(self) -> None:
        current = split_and_rerank_classes(
            [RankEntry(1, "202607230601", "实时", 10, 12, 83.3, "L1")], "2026"
        )
        merged, historical_ids = merge_class_history(
            current,
            [
                HistoricalRankEntry(
                    RankEntry(7, "202607230602", "历史", 2, 3, 66.7, "L1"),
                    "2026-08-23T08:00:00+08:00",
                )
            ],
            "2026",
        )

        self.assertEqual(
            [("202607230601", 1), ("202607230602", 0)],
            [(entry.user_id, entry.rank) for entry in merged["2026072306"]],
        )
        self.assertEqual(frozenset({"202607230602"}), historical_ids["2026072306"])

    def test_major_split_uses_first_eight_digits_and_local_ranks(self) -> None:
        entries = [
            RankEntry(1, "202607230612", "A", 10, 10, 100.0, "L1"),
            RankEntry(2, "202607240611", "B", 9, 10, 90.0, "L1"),
            RankEntry(3, "202607230613", "C", 8, 10, 80.0, "L1"),
        ]

        majors = split_and_rerank_majors(entries, "2026")

        self.assertEqual(["20260723", "20260724"], list(majors))
        self.assertEqual([1, 2], [entry.rank for entry in majors["20260723"]])


    def test_maps_known_majors_and_leaves_unknown_codes_numeric(self) -> None:
        self.assertEqual("示例甲", major_display_name("20260723", "2026"))
        self.assertEqual("示例甲06班", class_display_name("2026072306", "2026"))
        self.assertEqual("示例乙", major_display_name("20260703", "2026"))
        self.assertEqual("示例乙01班", class_display_name("2026070301", "2026"))


    def test_builds_class_and_major_intensity_from_accepted_totals(self) -> None:
        entries = [
            RankEntry(1, "202607230601", "甲", 10, 12, 83.3, "L1"),
            RankEntry(2, "202607230602", "乙", 5, 8, 62.5, "L1"),
            RankEntry(3, "202607240601", "丙", 20, 22, 90.9, "L1"),
            RankEntry(4, "202607240602", "丁", 1, 3, 33.3, "L1"),
        ]
        classes = build_class_intensity(entries, "2026")
        majors = build_major_intensity(entries, "2026")

        self.assertEqual(["072406", "示例甲06班"], [row.user_id for row in classes])
        self.assertEqual([21, 15], [row.accepted for row in classes])
        self.assertEqual("丙（20 AC）", classes[0].nickname)
        self.assertEqual(["0724", "示例甲"], [row.user_id for row in majors])
        self.assertEqual([21, 15], [row.accepted for row in majors])
        self.assertEqual("072406｜丙（20 AC）", majors[0].nickname)
