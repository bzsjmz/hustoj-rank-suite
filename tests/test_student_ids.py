from __future__ import annotations

import unittest

from app.college import split_and_rerank_classes, split_and_rerank_majors
from app.models import RankEntry
from app.student_ids import StudentIdLayout


class StudentIdLayoutTests(unittest.TestCase):
    def test_custom_layout_extracts_major_and_class(self):
        layout = StudentIdLayout(
            prefix="UG",
            student_id_length=10,
            major_code_length=3,
            class_code_length=5,
            student_id_pattern=r"[A-Z0-9]+",
        )
        student_id = "UGC0101A01"
        self.assertTrue(layout.is_student_id(student_id))
        self.assertEqual("UGC01", layout.major_id(student_id))
        self.assertEqual("UGC0101", layout.class_id(student_id))

        entry = RankEntry(1, student_id, "示例", 3, 4, 75.0, "L1")
        self.assertEqual(["UGC01"], list(split_and_rerank_majors([entry], layout)))
        self.assertEqual(["UGC0101"], list(split_and_rerank_classes([entry], layout)))

    def test_layout_rejects_ambiguous_or_incomplete_segments(self):
        with self.assertRaises(ValueError):
            StudentIdLayout("26", 8, 4, 4)
        with self.assertRaises(ValueError):
            StudentIdLayout("26", 7, 3, 5)
        with self.assertRaises(ValueError):
            StudentIdLayout("bad/slash", 12, 3, 5, r"[A-Za-z/]+")


if __name__ == "__main__":
    unittest.main()
