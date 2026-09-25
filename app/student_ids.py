from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class StudentIdLayout:
    """Configurable, path-safe student-ID segmentation used by statistics."""

    prefix: str
    student_id_length: int
    major_code_length: int
    class_code_length: int
    student_id_pattern: str = r"[0-9]+"

    def __post_init__(self) -> None:
        try:
            re.compile(self.student_id_pattern)
        except re.error as exc:
            raise ValueError("invalid student ID pattern") from exc
        if not self.prefix or not re.fullmatch(r"[A-Za-z0-9_-]+", self.prefix):
            raise ValueError("student ID prefix contains unsafe characters")
        if not re.fullmatch(self.student_id_pattern, self.prefix):
            raise ValueError("student ID prefix does not match STUDENT_ID_PATTERN")
        if self.student_id_length <= len(self.prefix):
            raise ValueError("student ID length must be longer than its prefix")
        if not 0 < self.major_code_length < self.class_code_length:
            raise ValueError("major code length must be shorter than class code length")
        if len(self.prefix) + self.class_code_length >= self.student_id_length:
            raise ValueError("class code must leave at least one student sequence digit")

    @classmethod
    def conventional(cls, prefix: str) -> "StudentIdLayout":
        """Return the legacy 4+4+2+2 layout for compatibility."""
        return cls(prefix, len(prefix) + 8, 4, 6, r"[0-9]+")

    @property
    def major_id_length(self) -> int:
        return len(self.prefix) + self.major_code_length

    @property
    def class_id_length(self) -> int:
        return len(self.prefix) + self.class_code_length

    @staticmethod
    def _is_safe_segment(value: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z0-9_-]+", value))

    def is_student_id(self, value: str) -> bool:
        return (
            len(value) == self.student_id_length
            and self._is_safe_segment(value)
            and bool(re.fullmatch(self.student_id_pattern, value))
            and value.startswith(self.prefix)
        )

    def is_major_id(self, value: str) -> bool:
        return len(value) == self.major_id_length and self._is_safe_segment(value) and bool(re.fullmatch(self.student_id_pattern, value)) and value.startswith(self.prefix)

    def is_class_id(self, value: str) -> bool:
        return len(value) == self.class_id_length and self._is_safe_segment(value) and bool(re.fullmatch(self.student_id_pattern, value)) and value.startswith(self.prefix)

    def major_id(self, student_id: str) -> str:
        if not self.is_student_id(student_id):
            raise ValueError(f"invalid student ID for configured layout: {student_id}")
        return student_id[: self.major_id_length]

    def class_id(self, student_id: str) -> str:
        if not self.is_student_id(student_id):
            raise ValueError(f"invalid student ID for configured layout: {student_id}")
        return student_id[: self.class_id_length]
