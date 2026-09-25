from __future__ import annotations

import csv
import gzip
import io
import json
import math
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import AbstractSet, Mapping, Sequence

from .statistics_store import StatisticsStore
from .college import MAJOR_DISPLAY_NAMES
from .student_ids import StudentIdLayout
from .models import HistoricalRankEntry, RankBaseline, RankEntry


class RankExporter:
    def __init__(self, share_dir: Path):
        self.share_dir = share_dir

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o644)
        temporary.replace(path)
        path.chmod(0o644)

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.chmod(0o644)
        temporary.replace(path)
        path.chmod(0o644)

    def export_academic_labels(self, layout: StudentIdLayout | str) -> None:
        """Publish ID layout and optional major labels for read-only consumers."""
        if isinstance(layout, str):
            layout = StudentIdLayout.conventional(layout)
        self.share_dir.mkdir(parents=True, exist_ok=True)
        self.share_dir.chmod(0o755)
        document = {
            "schema_version": 1,
            "prefix": layout.prefix,
            "student_id_length": layout.student_id_length,
            "major_code_length": layout.major_code_length,
            "class_code_length": layout.class_code_length,
            "student_id_pattern": layout.student_id_pattern,
            "major_labels": {code: label for code, label in sorted(MAJOR_DISPLAY_NAMES.items()) if len(code) == layout.major_code_length},
        }
        content = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(self.share_dir / "academic-labels.json", content + "\n")


    def export(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
        snapshot_id: int,
        baseline: RankBaseline | None = None,
        historical_roster_user_ids: AbstractSet[str] | None = None,
    ) -> None:
        if not entries:
            raise ValueError("refusing to export an empty ranklist")
        current_user_ids = {entry.user_id for entry in entries}
        roster_user_ids = (
            frozenset(historical_roster_user_ids)
            if historical_roster_user_ids is not None
            else frozenset(current_user_ids)
        )
        if not roster_user_ids:
            raise ValueError("refusing to export an empty historical roster")
        if not current_user_ids.issubset(roster_user_ids):
            raise ValueError("historical roster does not contain the current ranklist")
        self.share_dir.mkdir(parents=True, exist_ok=True)
        self.share_dir.chmod(0o755)

        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": prefix,
            "user_count": len(entries),
            "historical_roster": {
                "user_count": len(roster_user_ids),
                "user_ids": sorted(roster_user_ids),
            },
            "current_user_count": len(current_user_ids),
            "missing_user_count": len(roster_user_ids - current_user_ids),
            "coverage_rate": len(current_user_ids) / len(roster_user_ids),
            "users": [asdict(entry) for entry in entries],
        }
        json_text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(self.share_dir / "latest.json", json_text + "\n")

        output = io.StringIO(newline="")
        fields = [
            "rank",
            "user_id",
            "nickname",
            "accepted",
            "submitted",
            "ratio",
            "level",
            "fetched_at",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for entry in entries:
            row = asdict(entry)
            row["fetched_at"] = fetched_at
            writer.writerow(row)
        self._atomic_write(self.share_dir / "latest.csv", output.getvalue())

        if baseline is not None:
            baseline_by_user = {entry.user_id: entry for entry in baseline.entries}
            changes = []
            for entry in entries:
                previous = baseline_by_user.get(entry.user_id)
                changes.append(
                    {
                        **asdict(entry),
                        "baseline_rank": previous.rank if previous else None,
                        "rank_change": previous.rank - entry.rank if previous else None,
                        "accepted_change": (
                            entry.accepted - previous.accepted if previous else None
                        ),
                        "submitted_change": (
                            entry.submitted - previous.submitted if previous else None
                        ),
                        "is_new": previous is None,
                    }
                )

            daily_document = {
                "schema_version": 1,
                "snapshot_id": snapshot_id,
                "fetched_at": fetched_at,
                "baseline_snapshot_id": baseline.snapshot_id,
                "baseline_fetched_at": baseline.fetched_at,
                "prefix": prefix,
                "user_count": len(entries),
                "changes": changes,
            }
            daily_text = json.dumps(
                daily_document, ensure_ascii=False, separators=(",", ":")
            )
            self._atomic_write(self.share_dir / "daily.json", daily_text + "\n")

    def export_statistics_snapshot(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
        snapshot_id: int,
        historical_roster_user_ids: AbstractSet[str],
    ) -> None:
        """Publish a compressed immutable snapshot for administrator-only exports."""
        if not entries or not historical_roster_user_ids:
            raise ValueError("refusing to export an empty statistics snapshot")
        # Historical snapshots can contain configured special accounts or malformed IDs.
        # They are intentionally absent from the student roster and must stay absent from
        # administrator statistics as well.
        entries = tuple(
            entry for entry in entries if entry.user_id in historical_roster_user_ids
        )
        if not entries:
            raise ValueError("statistics snapshot has no roster students")
        snapshot_dir = self.share_dir / "statistics-snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        snapshot_dir.chmod(0o755)
        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": prefix,
            "user_count": len(entries),
            "historical_roster": {
                "user_count": len(historical_roster_user_ids),
                "user_ids": sorted(historical_roster_user_ids),
            },
            "users": [asdict(entry) for entry in entries],
        }
        StatisticsStore(self.share_dir).publish(document)

    def export_lookup(
        self,
        entries: Sequence[RankEntry],
        fetched_at: str,
        snapshot_id: int,
        prefixes: Sequence[str],
        historical_entries: Sequence[HistoricalRankEntry] = (),
        prefix_fetched_at: Mapping[str, str] | None = None,
        first_seen_names: Mapping[str, str] | None = None,
    ) -> None:
        """Publish current multi-cohort rows plus marked historical fallbacks."""
        if not entries:
            raise ValueError("refusing to export an empty lookup dataset")
        if not prefixes or any(not re.fullmatch(r"[A-Za-z0-9_-]+", prefix) for prefix in prefixes):
            raise ValueError("invalid lookup prefixes")
        current_user_ids = [entry.user_id for entry in entries]
        if len(current_user_ids) != len(set(current_user_ids)):
            raise ValueError("duplicate user_id in lookup dataset")
        current_user_id_set = set(current_user_ids)
        fallback_entries = [
            historical
            for historical in historical_entries
            if historical.entry.user_id not in current_user_id_set
        ]
        fallback_user_ids = [historical.entry.user_id for historical in fallback_entries]
        if len(fallback_user_ids) != len(set(fallback_user_ids)):
            raise ValueError("duplicate historical user_id in lookup dataset")

        self.share_dir.mkdir(parents=True, exist_ok=True)
        self.share_dir.chmod(0o755)
        users = [asdict(entry) for entry in entries]
        users.extend(
            {
                **asdict(historical.entry),
                "is_historical": True,
                "last_seen_at": historical.fetched_at,
            }
            for historical in sorted(
                fallback_entries, key=lambda item: item.entry.user_id
            )
        )
        for row in users:
            name = (first_seen_names or {}).get(row["user_id"])
            if name is not None:
                if not isinstance(name, str):
                    raise ValueError("invalid first-seen name")
                row["first_seen_name"] = name
        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": "-".join(prefixes),
            "lookup_prefixes": list(prefixes),
            "prefix_fetched_at": {
                prefix: (prefix_fetched_at or {}).get(prefix, fetched_at)
                for prefix in prefixes
                if (prefix_fetched_at is not None and prefix in prefix_fetched_at)
                or (prefix_fetched_at is None and any(entry.user_id.startswith(prefix) for entry in entries))
            },
            "user_count": len(users),
            "users": users,
        }
        content = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(self.share_dir / "lookup.json", content + "\n")

    def load_lookup_current_entries(
        self, prefixes: Sequence[str]
    ) -> tuple[dict[str, list[RankEntry]], dict[str, str]]:
        """Load cached current rows while excluding historical fallbacks."""
        cached_entries = {prefix: [] for prefix in prefixes}
        prefix_fetched_at: dict[str, str] = {}
        path = self.share_dir / "lookup.json"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return cached_entries, prefix_fetched_at
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            return cached_entries, prefix_fetched_at

        raw_times = document.get("prefix_fetched_at")
        if isinstance(raw_times, dict):
            for prefix in prefixes:
                value = raw_times.get(prefix)
                if isinstance(value, str) and value:
                    try:
                        datetime.fromisoformat(value)
                    except ValueError:
                        continue
                    prefix_fetched_at[prefix] = value

        users = document.get("users")
        if not isinstance(users, list):
            return cached_entries, prefix_fetched_at
        seen_user_ids: set[str] = set()
        for row in users:
            if not isinstance(row, dict) or row.get("is_historical"):
                continue
            user_id = str(row.get("user_id", ""))
            prefix = next((item for item in prefixes if user_id.startswith(item)), None)
            if prefix is None or user_id in seen_user_ids:
                continue
            try:
                entry = RankEntry(
                    rank=int(row["rank"]),
                    user_id=user_id,
                    nickname=str(row.get("nickname", "")),
                    accepted=int(row["accepted"]),
                    submitted=int(row["submitted"]),
                    ratio=float(row["ratio"]),
                    level=str(row.get("level", "")),
                )
            except (KeyError, TypeError, ValueError):
                continue
            seen_user_ids.add(user_id)
            cached_entries[prefix].append(entry)
        prefix_fetched_at = {key: value for key, value in prefix_fetched_at.items() if cached_entries[key]}
        return cached_entries, prefix_fetched_at

    def export_scoped(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
        scope: str,
        snapshot_id: int = 0,
    ) -> None:
        filenames = {"w": "weekly.json", "m": "monthly.json"}
        if scope not in filenames:
            raise ValueError(f"unsupported rank scope: {scope}")
        self.share_dir.mkdir(parents=True, exist_ok=True)
        self.share_dir.chmod(0o755)
        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": prefix,
            "scope": scope,
            "user_count": len(entries),
            "users": [asdict(entry) for entry in entries],
        }
        content = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(self.share_dir / filenames[scope], content + "\n")

    def export_college(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
        snapshot_id: int,
        college: str,
        split_snapshot_id: int,
    ) -> None:
        filenames = {"计院": "computer-college.json", "软院": "software-college.json"}
        filename = filenames.get(college)
        if filename is None:
            raise ValueError(f"unsupported college: {college}")
        # An empty derived college is valid; publish its empty membership so a
        # complete generation never silently retains an old college roster.
        self.share_dir.mkdir(parents=True, exist_ok=True)
        self.share_dir.chmod(0o755)
        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": prefix,
            "scope": "college",
            "college": college,
            "split_snapshot_id": split_snapshot_id,
            "user_count": len(entries),
            "users": [asdict(entry) for entry in entries],
        }
        content = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(self.share_dir / filename, content + "\n")


    def export_class(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
        snapshot_id: int,
        class_id: str,
        historical_user_ids: AbstractSet[str] = frozenset(),
        layout: StudentIdLayout | None = None,
    ) -> None:
        layout = layout or StudentIdLayout(prefix, len(prefix) + 8, 4, 6)
        if not layout.is_class_id(class_id):
            raise ValueError(f"invalid class ID: {class_id}")
        if not entries:
            raise ValueError(f"refusing to export an empty class leaderboard: {class_id}")
        directory = self.share_dir / "class-ranklists"
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)
        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": prefix,
            "scope": "class",
            "class_id": class_id,
            "user_count": len(entries),
            "current_user_count": len(entries) - len(historical_user_ids),
            "historical_user_count": len(historical_user_ids),
            "users": [
                {
                    **asdict(entry),
                    "is_historical": entry.user_id in historical_user_ids,
                }
                for entry in entries
            ],
        }
        content = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(directory / f"{class_id}.json", content + "\n")


    def export_major(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
        snapshot_id: int,
        major_id: str,
        layout: StudentIdLayout | None = None,
    ) -> None:
        layout = layout or StudentIdLayout(prefix, len(prefix) + 8, 4, 6)
        if not layout.is_major_id(major_id):
            raise ValueError(f"invalid major ID: {major_id}")
        if not entries:
            raise ValueError(f"refusing to export an empty major leaderboard: {major_id}")
        directory = self.share_dir / "major-ranklists"
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755)
        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": prefix,
            "scope": "major",
            "major_id": major_id,
            "user_count": len(entries),
            "users": [asdict(entry) for entry in entries],
        }
        content = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(directory / f"{major_id}.json", content + "\n")

    def export_entity_registry(
        self,
        kind: str,
        entries_by_id: dict[str, Sequence[RankEntry]],
        layout: StudentIdLayout,
        fetched_at: str,
        snapshot_id: int,
    ) -> None:
        if kind not in {"class", "major"} or not entries_by_id:
            raise ValueError("invalid or empty entity registry")
        key = "classes" if kind == "class" else "majors"
        id_key = "class_id" if kind == "class" else "major_id"
        validator = layout.is_class_id if kind == "class" else layout.is_major_id
        items = []
        for entity_id, entries in sorted(entries_by_id.items()):
            if not validator(entity_id) or not entries:
                raise ValueError(f"invalid {kind} registry entry: {entity_id}")
            items.append(
                {
                    id_key: entity_id,
                    "user_count": len(entries),
                    "page_count": 1 if kind == "class" else math.ceil(len(entries) / 20),
                }
            )
        root = self.share_dir / f"{kind}-images"
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o755)
        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": layout.prefix,
            f"{kind}_id_length": (
                layout.class_id_length if kind == "class" else layout.major_id_length
            ),
            key: items,
        }
        content = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(root / "manifest.json", content + "\n")

    def export_intensity(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
        snapshot_id: int,
        kind: str,
    ) -> None:
        filenames = {
            "class": "class-intensity.json",
            "major": "major-intensity.json",
        }
        filename = filenames.get(kind)
        if filename is None or not entries:
            raise ValueError("invalid or empty intensity leaderboard")
        document = {
            "schema_version": 1,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": prefix,
            "scope": f"{kind}-intensity",
            "user_count": len(entries),
            "users": [asdict(entry) for entry in entries],
        }
        content = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        self._atomic_write(self.share_dir / filename, content + "\n")
