from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import AbstractSet, Sequence, Iterator

from .models import HistoricalRankEntry, RankBaseline, RankEntry, validate_rank_entry
from .ranking import exclude_and_rerank
from .student_ids import StudentIdLayout


SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,
    prefix TEXT NOT NULL,
    user_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS current_rank (
    prefix TEXT NOT NULL,
    user_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    submitted INTEGER NOT NULL,
    ratio REAL NOT NULL,
    level TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (prefix, user_id)
);

CREATE TABLE IF NOT EXISTS rank_snapshots (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    submitted INTEGER NOT NULL,
    ratio REAL NOT NULL,
    level TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_prefix_time
ON snapshots(prefix, fetched_at);

CREATE INDEX IF NOT EXISTS idx_rank_snapshots_user
ON rank_snapshots(user_id, snapshot_id);

CREATE TABLE IF NOT EXISTS student_roster (
    prefix TEXT NOT NULL,
    user_id TEXT NOT NULL,
    first_seen_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    last_seen_snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    last_seen_at TEXT NOT NULL,
    rank INTEGER NOT NULL,
    nickname TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    submitted INTEGER NOT NULL,
    ratio REAL NOT NULL,
    level TEXT NOT NULL,
    PRIMARY KEY (prefix, user_id)
);

CREATE INDEX IF NOT EXISTS idx_student_roster_prefix
ON student_roster(prefix, user_id);
"""


class RankDatabase:
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = path
        self.read_only = read_only

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = (sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
                      if self.read_only else sqlite3.connect(self.path, timeout=30))
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if self.read_only:
                connection.execute("PRAGMA query_only = ON")
            else:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        if self.read_only:
            raise ValueError("cannot initialize a read-only database")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            roster_count = int(
                connection.execute("SELECT COUNT(*) FROM student_roster").fetchone()[0]
            )
            snapshot_count = int(
                connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            )
            if roster_count == 0 and snapshot_count > 0:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    WITH roster_bounds AS (
                        SELECT snapshots.prefix AS prefix,
                               rank_snapshots.user_id AS user_id,
                               MIN(rank_snapshots.snapshot_id) AS first_snapshot_id,
                               MAX(rank_snapshots.snapshot_id) AS last_snapshot_id
                        FROM rank_snapshots
                        INNER JOIN snapshots
                            ON snapshots.id = rank_snapshots.snapshot_id
                        GROUP BY snapshots.prefix, rank_snapshots.user_id
                    )
                    INSERT INTO student_roster(
                        prefix, user_id, first_seen_snapshot_id,
                        last_seen_snapshot_id, last_seen_at, rank, nickname,
                        accepted, submitted, ratio, level
                    )
                    SELECT roster_bounds.prefix, roster_bounds.user_id,
                           roster_bounds.first_snapshot_id,
                           roster_bounds.last_snapshot_id,
                           snapshots.fetched_at, rank_snapshots.rank,
                           rank_snapshots.nickname, rank_snapshots.accepted,
                           rank_snapshots.submitted, rank_snapshots.ratio,
                           rank_snapshots.level
                    FROM roster_bounds
                    INNER JOIN rank_snapshots
                        ON rank_snapshots.snapshot_id = roster_bounds.last_snapshot_id
                       AND rank_snapshots.user_id = roster_bounds.user_id
                    INNER JOIN snapshots
                        ON snapshots.id = roster_bounds.last_snapshot_id
                    """
                )

    def save_complete_snapshot(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
    ) -> int:
        if not entries:
            raise ValueError("refusing to replace current_rank with an empty result")
        for entry in entries:
            validate_rank_entry(entry)
        user_ids = [entry.user_id for entry in entries]
        if len(user_ids) != len(set(user_ids)):
            raise ValueError("duplicate user_id in complete scrape")

        snapshot_rows = [
            (
                entry.user_id,
                entry.rank,
                entry.nickname,
                entry.accepted,
                entry.submitted,
                entry.ratio,
                entry.level,
            )
            for entry in entries
        ]
        current_rows = [
            (
                prefix,
                entry.user_id,
                entry.rank,
                entry.nickname,
                entry.accepted,
                entry.submitted,
                entry.ratio,
                entry.level,
                fetched_at,
            )
            for entry in entries
        ]

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT INTO snapshots(fetched_at, prefix, user_count) VALUES (?, ?, ?)",
                (fetched_at, prefix, len(entries)),
            )
            snapshot_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO rank_snapshots(
                    snapshot_id, user_id, rank, nickname, accepted,
                    submitted, ratio, level
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(snapshot_id, *row) for row in snapshot_rows],
            )
            connection.executemany(
                """
                INSERT INTO student_roster(
                    prefix, user_id, first_seen_snapshot_id,
                    last_seen_snapshot_id, last_seen_at, rank, nickname,
                    accepted, submitted, ratio, level
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(prefix, user_id) DO UPDATE SET
                    last_seen_snapshot_id = excluded.last_seen_snapshot_id,
                    last_seen_at = excluded.last_seen_at,
                    rank = excluded.rank,
                    nickname = excluded.nickname,
                    accepted = excluded.accepted,
                    submitted = excluded.submitted,
                    ratio = excluded.ratio,
                    level = excluded.level
                """,
                [
                    (
                        prefix, entry.user_id, snapshot_id, snapshot_id, fetched_at,
                        entry.rank, entry.nickname, entry.accepted, entry.submitted,
                        entry.ratio, entry.level,
                    )
                    for entry in entries
                ],
            )
            connection.execute("DELETE FROM current_rank WHERE prefix = ?", (prefix,))
            connection.executemany(
                """
                INSERT INTO current_rank(
                    prefix, user_id, rank, nickname, accepted,
                    submitted, ratio, level, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                current_rows,
            )
        return snapshot_id

    def current_count(self, prefix: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM current_rank WHERE prefix = ?", (prefix,)
            ).fetchone()
        return int(row[0])
    def historical_roster_user_ids(
        self, prefix: str, excluded_user_ids: AbstractSet[str] = frozenset(),
        student_id_length: int | None = None,
        student_id_layout: StudentIdLayout | None = None,
    ) -> frozenset[str]:
        """Return every non-excluded student ever present in a committed snapshot.

        ``rank_snapshots`` is immutable historical data, so this roster is naturally
        append-only: a student missing from a later ranklist remains in the result.
        Only complete IDs matching the configured layout are treated as students;
        configured test/special accounts are also kept out of the user-facing roster.
        """
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id
                FROM student_roster
                WHERE prefix = ?
                """,
                (prefix,),
            ).fetchall()
        return frozenset(
            user_id
            for row in rows
            for user_id in (str(row[0]),)
            if (
                user_id not in excluded_user_ids
                and (
                    student_id_layout.is_student_id(user_id)
                    if student_id_layout is not None
                    else (
                        user_id.startswith(prefix)
                        and len(user_id) == (student_id_length or len(prefix) + 8)
                        and user_id.isdigit()
                    )
                )
            )
        )

    def first_seen_names(self, prefix: str, user_ids: AbstractSet[str]) -> dict[str, str]:
        """Resolve roster identities through immutable first snapshots, never latest nicknames."""
        if not user_ids:
            return {}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT roster.user_id, first.nickname
                FROM student_roster AS roster
                JOIN rank_snapshots AS first
                  ON first.snapshot_id = roster.first_seen_snapshot_id
                 AND first.user_id = roster.user_id
                WHERE roster.prefix = ?
                """, (prefix,),
            ).fetchall()
        return {str(uid): str(name) for uid, name in rows if uid in user_ids}

    def latest_entries_for_user_ids(
        self, prefix: str, user_ids: AbstractSet[str]
    ) -> dict[str, HistoricalRankEntry]:
        """Return the most recent saved row for each requested historical user."""
        if not user_ids:
            return {}

        latest: dict[str, HistoricalRankEntry] = {}
        ordered_user_ids = sorted(user_ids)
        with self._connect() as connection:
            for offset in range(0, len(ordered_user_ids), 900):
                batch = ordered_user_ids[offset : offset + 900]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"""
                    SELECT user_id, rank, nickname, accepted, submitted,
                           ratio, level, last_seen_at
                    FROM student_roster
                    WHERE prefix = ? AND user_id IN ({placeholders})
                    """,
                    (prefix, *batch),
                ).fetchall()
                for row in rows:
                    user_id = str(row[0])
                    latest[user_id] = HistoricalRankEntry(
                        entry=RankEntry(
                            rank=int(row[1]),
                            user_id=user_id,
                            nickname=str(row[2]),
                            accepted=int(row[3]),
                            submitted=int(row[4]),
                            ratio=float(row[5]),
                            level=str(row[6]),
                        ),
                        fetched_at=str(row[7]),
                    )
        return latest

    def snapshot_user_ids(self, snapshot_id: int, prefix: str) -> frozenset[str]:
        """Return the complete user roster from a verified historical snapshot."""
        with self._connect() as connection:
            snapshot = connection.execute(
                "SELECT prefix, user_count FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if snapshot is None:
                raise ValueError(f"snapshot {snapshot_id} not found")
            if str(snapshot[0]) != prefix:
                raise ValueError(
                    f"snapshot {snapshot_id} prefix does not match configured prefix"
                )
            rows = connection.execute(
                "SELECT user_id FROM rank_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchall()

        user_ids = frozenset(str(row[0]) for row in rows)
        if not user_ids or len(user_ids) != int(snapshot[1]):
            raise ValueError(f"snapshot {snapshot_id} roster is incomplete")
        return user_ids

    def snapshot_entries(self, snapshot_id: int, prefix: str) -> tuple[RankEntry, ...]:
        """Read one committed snapshot without exposing the database to consumers."""
        with self._connect() as connection:
            snapshot = connection.execute(
                "SELECT prefix, user_count FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if snapshot is None:
                raise ValueError(f"snapshot {snapshot_id} not found")
            if str(snapshot[0]) != prefix:
                raise ValueError(
                    f"snapshot {snapshot_id} prefix does not match configured prefix"
                )
            rows = connection.execute(
                """
                SELECT rank, user_id, nickname, accepted, submitted, ratio, level
                FROM rank_snapshots
                WHERE snapshot_id = ?
                ORDER BY rank ASC, user_id ASC
                """,
                (snapshot_id,),
            ).fetchall()
        if not rows or len(rows) != int(snapshot[1]):
            raise ValueError(f"snapshot {snapshot_id} data is incomplete")
        return tuple(
            RankEntry(
                rank=int(row[0]), user_id=str(row[1]), nickname=str(row[2]),
                accepted=int(row[3]), submitted=int(row[4]), ratio=float(row[5]),
                level=str(row[6]),
            )
            for row in rows
        )

    def historical_roster_user_ids_at_snapshot(
        self,
        snapshot_id: int,
        prefix: str,
        excluded_user_ids: AbstractSet[str] = frozenset(),
        student_id_layout: StudentIdLayout | None = None,
    ) -> frozenset[str]:
        """Return the append-only roster as it existed at a committed snapshot."""
        with self._connect() as connection:
            snapshot = connection.execute(
                "SELECT prefix FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if snapshot is None or str(snapshot[0]) != prefix:
                raise ValueError(f"snapshot {snapshot_id} does not belong to prefix {prefix}")
            rows = connection.execute(
                """
                SELECT DISTINCT rank_snapshots.user_id
                FROM rank_snapshots
                INNER JOIN snapshots ON snapshots.id = rank_snapshots.snapshot_id
                WHERE snapshots.prefix = ? AND rank_snapshots.snapshot_id <= ?
                """,
                (prefix, snapshot_id),
            ).fetchall()
        return frozenset(
            user_id
            for row in rows
            for user_id in (str(row[0]),)
            if user_id not in excluded_user_ids
            and (
                student_id_layout.is_student_id(user_id)
                if student_id_layout is not None
                else user_id.startswith(prefix) and user_id.isdigit()
            )
        )

    def roster_first_seen(self, prefix: str) -> tuple[tuple[int, str], ...]:
        """Small roster lookup, avoiding repeated scans of full rank history."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT first_seen_snapshot_id, user_id FROM student_roster "
                "WHERE prefix = ? ORDER BY first_seen_snapshot_id, user_id", (prefix,)
            ).fetchall()
        return tuple((int(row[0]), str(row[1])) for row in rows)

    def snapshot_metadata(self, prefix: str) -> tuple[tuple[int, str, int], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, fetched_at, user_count
                FROM snapshots
                WHERE prefix = ?
                ORDER BY id ASC
                """,
                (prefix,),
            ).fetchall()
        return tuple((int(row[0]), str(row[1]), int(row[2])) for row in rows)

    def daily_baseline(
        self,
        prefix: str,
        fetched_at: str,
        current_snapshot_id: int,
        excluded_user_ids: AbstractSet[str],
    ) -> RankBaseline:
        current_time = datetime.fromisoformat(fetched_at)
        day_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        with self._connect() as connection:
            snapshot = connection.execute(
                """
                SELECT id, fetched_at
                FROM snapshots
                WHERE prefix = ?
                  AND fetched_at >= ?
                  AND fetched_at < ?
                  AND id <= ?
                ORDER BY fetched_at ASC, id ASC
                LIMIT 1
                """,
                (
                    prefix,
                    day_start.isoformat(timespec="seconds"),
                    day_end.isoformat(timespec="seconds"),
                    current_snapshot_id,
                ),
            ).fetchone()
            if snapshot is None:
                raise ValueError("daily baseline snapshot not found")

            rows = connection.execute(
                """
                SELECT rank, user_id, nickname, accepted, submitted, ratio, level
                FROM rank_snapshots
                WHERE snapshot_id = ?
                ORDER BY rank ASC, user_id ASC
                """,
                (int(snapshot[0]),),
            ).fetchall()

        entries = [
            RankEntry(
                rank=int(row[0]),
                user_id=str(row[1]),
                nickname=str(row[2]),
                accepted=int(row[3]),
                submitted=int(row[4]),
                ratio=float(row[5]),
                level=str(row[6]),
            )
            for row in rows
        ]
        normalized = exclude_and_rerank(entries, excluded_user_ids)
        if not normalized:
            raise ValueError("daily baseline is empty after exclusions")
        return RankBaseline(
            snapshot_id=int(snapshot[0]),
            fetched_at=str(snapshot[1]),
            entries=tuple(normalized),
        )
