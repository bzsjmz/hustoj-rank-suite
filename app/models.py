from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RankEntry:
    rank: int
    user_id: str
    nickname: str
    accepted: int
    submitted: int
    ratio: float
    level: str


@dataclass(frozen=True)
class RankBaseline:
    snapshot_id: int
    fetched_at: str
    entries: tuple[RankEntry, ...]


@dataclass(frozen=True)
class HistoricalRankEntry:
    entry: RankEntry
    fetched_at: str


def validate_rank_entry(entry: RankEntry) -> None:
    """Reject malformed programmatic entries before a snapshot transaction."""
    for field, minimum in (("rank", 1), ("accepted", 0), ("submitted", 0)):
        value = getattr(entry, field)
        if type(value) is not int or value < minimum:
            raise ValueError(f"invalid {field} domain or type")
    if type(entry.ratio) not in (int, float) or not math.isfinite(entry.ratio) or not 0 <= entry.ratio <= 100:
        raise ValueError("invalid ratio domain or type")
    if not isinstance(entry.user_id, str) or not entry.user_id.strip():
        raise ValueError("invalid user_id")
    if not isinstance(entry.nickname, str) or not isinstance(entry.level, str):
        raise ValueError("nickname and level must be strings")
