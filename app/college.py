from __future__ import annotations

from dataclasses import replace
from typing import AbstractSet, Sequence

from .models import HistoricalRankEntry, RankEntry
from .student_ids import StudentIdLayout


COMPUTER_COLLEGE = "计院"
SOFTWARE_COLLEGE = "软院"


def _layout(value: StudentIdLayout | str) -> StudentIdLayout:
    return value if isinstance(value, StudentIdLayout) else StudentIdLayout.conventional(value)


def _configured_major_labels():
    import json
    import os
    from pathlib import Path
    from .config import PROJECT_ROOT  # Load deployment .env before optional mapping.
    name = os.getenv("OJ_MAJOR_LABELS_FILE", "").strip()
    if not name:
        return {}
    data = json.loads(Path(name).expanduser().read_text())
    if not isinstance(data, dict) or any(not isinstance(k, str) or not k.isdigit()
            or not isinstance(v, str) or not v.strip() for k, v in data.items()):
        raise ValueError("invalid private major label mapping")
    return data


MAJOR_DISPLAY_NAMES = _configured_major_labels()


def split_and_rerank_colleges(entries: Sequence[RankEntry], computer_college_user_ids: AbstractSet[str]) -> tuple[list[RankEntry], list[RankEntry]]:
    """Split current entries using a roster frozen at a configured snapshot."""
    computer = [entry for entry in entries if entry.user_id in computer_college_user_ids]
    software = [entry for entry in entries if entry.user_id not in computer_college_user_ids]
    return (
        [replace(entry, rank=rank) for rank, entry in enumerate(computer, start=1)],
        [replace(entry, rank=rank) for rank, entry in enumerate(software, start=1)],
    )


def split_and_rerank_classes(entries: Sequence[RankEntry], layout: StudentIdLayout | str) -> dict[str, list[RankEntry]]:
    layout = _layout(layout)
    grouped: dict[str, list[RankEntry]] = {}
    for entry in entries:
        try:
            class_id = layout.class_id(entry.user_id)
        except ValueError as exc:
            raise ValueError(f"user_id cannot be assigned to a class: {entry.user_id}") from exc
        grouped.setdefault(class_id, []).append(entry)
    return {class_id: [replace(entry, rank=rank) for rank, entry in enumerate(grouped[class_id], start=1)] for class_id in sorted(grouped)}


def merge_class_history(current_ranklists: dict[str, list[RankEntry]], historical_entries: Sequence[HistoricalRankEntry], layout: StudentIdLayout | str) -> tuple[dict[str, list[RankEntry]], dict[str, frozenset[str]]]:
    layout = _layout(layout)
    """Append missing historical users without changing current local ranks."""
    merged = {class_id: list(entries) for class_id, entries in current_ranklists.items()}
    current_user_ids = {entry.user_id for entries in current_ranklists.values() for entry in entries}
    historical_by_class: dict[str, list[RankEntry]] = {}
    for historical in historical_entries:
        entry = historical.entry
        if entry.user_id in current_user_ids:
            continue
        try:
            class_id = layout.class_id(entry.user_id)
        except ValueError as exc:
            raise ValueError(f"historical user_id cannot be assigned to a class: {entry.user_id}") from exc
        historical_by_class.setdefault(class_id, []).append(replace(entry, rank=0))
    historical_user_ids: dict[str, frozenset[str]] = {}
    for class_id, historical_class_entries in historical_by_class.items():
        ordered = sorted(historical_class_entries, key=lambda entry: entry.user_id)
        merged.setdefault(class_id, []).extend(ordered)
        historical_user_ids[class_id] = frozenset(entry.user_id for entry in ordered)
    return dict(sorted(merged.items())), historical_user_ids


def split_and_rerank_majors(entries: Sequence[RankEntry], layout: StudentIdLayout | str) -> dict[str, list[RankEntry]]:
    layout = _layout(layout)
    grouped: dict[str, list[RankEntry]] = {}
    for entry in entries:
        try:
            major_id = layout.major_id(entry.user_id)
        except ValueError as exc:
            raise ValueError(f"user_id cannot be assigned to a major: {entry.user_id}") from exc
        grouped.setdefault(major_id, []).append(entry)
    return {major_id: [replace(entry, rank=rank) for rank, entry in enumerate(grouped[major_id], start=1)] for major_id in sorted(grouped)}


def major_display_name(major_id: str, layout: StudentIdLayout | str) -> str:
    layout = _layout(layout)
    if not layout.is_major_id(major_id):
        raise ValueError(f"invalid major ID: {major_id}")
    code = major_id[len(layout.prefix):]
    return MAJOR_DISPLAY_NAMES.get(code, code)


def class_display_name(class_id: str, layout: StudentIdLayout | str) -> str:
    layout = _layout(layout)
    if not layout.is_class_id(class_id):
        raise ValueError(f"invalid class ID: {class_id}")
    major_id = class_id[:layout.major_id_length]
    major_name = major_display_name(major_id, layout)
    class_code = class_id[len(layout.prefix):]
    major_code = major_id[len(layout.prefix):]
    class_suffix = class_code[layout.major_code_length:]
    return class_code if major_name == major_code else f"{major_name}{class_suffix}班"


def _top_entry(entries: Sequence[RankEntry]) -> RankEntry:
    if not entries:
        raise ValueError("cannot choose a champion from an empty group")
    return min(entries, key=lambda entry: (-entry.accepted, entry.rank, entry.user_id))


def build_class_intensity(entries: Sequence[RankEntry], layout: StudentIdLayout | str) -> list[RankEntry]:
    layout = _layout(layout)
    classes = split_and_rerank_classes(entries, layout)
    rows: list[tuple[str, int, RankEntry]] = []
    for class_id, class_entries in classes.items():
        champion = _top_entry(class_entries)
        rows.append((class_id, sum(entry.accepted for entry in class_entries), champion))
    rows.sort(key=lambda row: (-row[1], row[0]))
    return [RankEntry(rank, class_display_name(class_id, layout), (champion.nickname or "未设置昵称") + f"（{champion.accepted} AC）", accepted, 0, 0.0, "") for rank, (class_id, accepted, champion) in enumerate(rows, start=1)]


def build_major_intensity(entries: Sequence[RankEntry], layout: StudentIdLayout | str) -> list[RankEntry]:
    layout = _layout(layout)
    classes = split_and_rerank_classes(entries, layout)
    grouped: dict[str, list[tuple[str, list[RankEntry]]]] = {}
    for class_id, class_entries in classes.items():
        grouped.setdefault(class_id[:layout.major_id_length], []).append((class_id, class_entries))
    rows: list[tuple[str, int, str, RankEntry]] = []
    for major_id, major_classes in grouped.items():
        accepted = sum(entry.accepted for _, class_entries in major_classes for entry in class_entries)
        leader_class_id, leader_class_entries = min(major_classes, key=lambda item: (-sum(entry.accepted for entry in item[1]), item[0]))
        rows.append((major_id, accepted, leader_class_id, _top_entry(leader_class_entries)))
    rows.sort(key=lambda row: (-row[1], row[0]))
    return [RankEntry(rank, major_display_name(major_id, layout), f"{class_display_name(leader_class_id, layout)}｜" + (champion.nickname or "未设置昵称") + f"（{champion.accepted} AC）", accepted, 0, 0.0, "") for rank, (major_id, accepted, leader_class_id, champion) in enumerate(rows, start=1)]
