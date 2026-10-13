"""Cached, replayable daily AC records derived only from complete snapshots."""
from bisect import bisect_right
from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")
TOLERANCE = timedelta(minutes=30)
SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_record_days (
    prefix TEXT NOT NULL, day TEXT NOT NULL, signature TEXT NOT NULL,
    PRIMARY KEY (prefix, day)
);
CREATE TABLE IF NOT EXISTS daily_ac_records (
    prefix TEXT NOT NULL, day TEXT NOT NULL, user_id TEXT NOT NULL,
    nickname TEXT NOT NULL, accepted_change INTEGER NOT NULL CHECK (accepted_change > 0),
    start_snapshot_id INTEGER NOT NULL, end_snapshot_id INTEGER NOT NULL,
    start_at TEXT NOT NULL, end_at TEXT NOT NULL,
    PRIMARY KEY (prefix, day, user_id)
);
"""


def local_time(value):
    stamp = datetime.fromisoformat(value)
    # Older offset-free snapshots follow the documented business timezone.
    return (stamp.replace(tzinfo=ZONE) if stamp.tzinfo is None else stamp.astimezone(ZONE))


def export_daily_records(database, exporter, prefix, fetched_at, snapshot_id,
                         excluded_user_ids, student_id_layout):
    """Backfill once, then recompute only days whose source signature changed.

    Boundaries use the latest snapshot at/before midnight (within 30 minutes).
    Any observed missing account or AC decrease within the interval invalidates
    that account's record. Unobserved corrections cannot be detected.
    """
    today = local_time(fetched_at).replace(hour=0, minute=0, second=0, microsecond=0)
    metadata = sorted(
        (local_time(stamp), sid, stamp)
        for sid, stamp, _ in database.snapshot_metadata(prefix)
        if sid <= snapshot_id and local_time(stamp) <= local_time(fetched_at)
    )
    stamps = [row[0] for row in metadata]
    with database._connect() as connection:
        connection.executescript(SCHEMA)
        cached = dict(connection.execute(
            "SELECT day, signature FROM daily_record_days WHERE prefix = ?", (prefix,)
        ))
        day = metadata[0][0].replace(hour=0, minute=0, second=0, microsecond=0) if metadata else today
        while day < today:
            end = day + timedelta(days=1)
            left = bisect_right(stamps, day) - 1
            right = bisect_right(stamps, end) - 1
            valid = (left >= 0 and right > left and
                     day - stamps[left] <= TOLERANCE and end - stamps[right] <= TOLERANCE)
            sources = metadata[left:right + 1] if valid else []
            signature = json.dumps([row[1] for row in sources])
            date = day.date().isoformat()
            if cached.get(date) != signature:
                connection.execute("DELETE FROM daily_ac_records WHERE prefix = ? AND day = ?", (prefix, date))
                if sources:
                    baseline = None
                    survivors = set()
                    previous = {}
                    final = {}
                    for _, sid, _ in sources:
                        current = {uid: (name, ac) for uid, name, ac in connection.execute(
                            "SELECT user_id, nickname, accepted FROM rank_snapshots WHERE snapshot_id = ?", (sid,)
                        )}
                        if baseline is None:
                            baseline = current
                            survivors = set(current)
                        else:
                            survivors &= current.keys()
                            survivors = {uid for uid in survivors if current[uid][1] >= previous[uid][1]}
                        previous = current
                        final = current
                    for uid in sorted(survivors):
                        gain = final[uid][1] - baseline[uid][1]
                        if gain > 0:
                            connection.execute(
                                "INSERT INTO daily_ac_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (prefix, date, uid, final[uid][0], gain, sources[0][1], sources[-1][1],
                                 sources[0][2], sources[-1][2]),
                            )
                connection.execute("INSERT OR REPLACE INTO daily_record_days VALUES (?, ?, ?)",
                                   (prefix, date, signature))
            day = end
        rows = connection.execute(
            "SELECT day, user_id, nickname, accepted_change, start_snapshot_id, end_snapshot_id, start_at, end_at "
            "FROM daily_ac_records WHERE prefix = ? AND day < ? "
            "ORDER BY accepted_change DESC, day ASC, user_id ASC", (prefix, today.date().isoformat())
        ).fetchall()
        connection.commit()
    users = []
    previous_gain = None
    rank = 0
    for date, uid, nickname, gain, start_id, end_id, start_at, end_at in rows:
        if uid in excluded_user_ids or not student_id_layout.is_student_id(uid):
            continue
        if gain != previous_gain:
            rank = len(users) + 1
        previous_gain = gain
        users.append(dict(rank=rank, user_id=uid, nickname=nickname, date=date,
                          accepted=gain, accepted_change=gain, submitted=0, ratio=0, level="",
                          start_snapshot_id=start_id, end_snapshot_id=end_id,
                          start_at=start_at, end_at=end_at))
    payload = dict(schema_version=1, board="historical-daily", snapshot_id=snapshot_id,
                   fetched_at=fetched_at, prefix=prefix, timezone="Asia/Shanghai",
                   boundary_tolerance_minutes=30, user_count=len(users), record_count=len(users),
                   users=users)
    exporter._atomic_write(exporter.share_dir / "historical-daily.json",
                           json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    return payload
