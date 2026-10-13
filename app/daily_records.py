"""Cached, replayable daily AC records derived only from complete snapshots."""
from bisect import bisect_right
from datetime import datetime, timedelta
import json
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .database import RankDatabase

ZONE = ZoneInfo("Asia/Shanghai")
TOLERANCE = timedelta(minutes=30)
SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_record_progress (
    prefix TEXT PRIMARY KEY, snapshot_id INTEGER NOT NULL, through_date TEXT NOT NULL
);
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


def update_daily_records(database, prefix, fetched_at, snapshot_id, *, pause=lambda: False,
                         throttle_seconds=0.05):
    """Rebuild in a worker, committing each day so interrupted work resumes."""
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
        if pause():
            return False
        if cached.get(date) != signature:
            with database._connect() as connection:
                records = []
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
                            records.append((prefix, date, uid, final[uid][0], gain,
                                            sources[0][1], sources[-1][1],
                                            sources[0][0].isoformat(), sources[-1][0].isoformat()))
                connection.execute("DELETE FROM daily_ac_records WHERE prefix = ? AND day = ?", (prefix, date))
                connection.executemany("INSERT INTO daily_ac_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", records)
                connection.execute("INSERT OR REPLACE INTO daily_record_days VALUES (?, ?, ?)",
                                   (prefix, date, signature))
        day = end
        time.sleep(throttle_seconds)
    with database._connect() as connection:
        connection.execute("INSERT OR REPLACE INTO daily_record_progress VALUES (?, ?, ?)",
                           (prefix, snapshot_id, today.date().isoformat()))
    return True


def export_daily_records(database, exporter, prefix, fetched_at, snapshot_id,
                         excluded_user_ids, student_id_layout):
    """Publish completed cache rows only; never scan raw historical snapshots."""
    today = local_time(fetched_at).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    cached_snapshot_id = 0
    through_date = ""
    reader = RankDatabase(database.path, read_only=True)
    with reader._connect() as connection:
        # Pin progress and records to one view while the worker commits days.
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "daily_record_progress" in tables:
            progress = connection.execute("SELECT snapshot_id, through_date FROM daily_record_progress WHERE prefix = ?", (prefix,)).fetchone()
            if progress:
                cached_snapshot_id, through_date = progress
        if "daily_ac_records" in tables:
            rows = connection.execute(
                "SELECT day, user_id, nickname, accepted_change, start_snapshot_id, end_snapshot_id, start_at, end_at "
                "FROM daily_ac_records WHERE prefix = ? AND day < ? AND end_snapshot_id <= ? "
                "ORDER BY accepted_change DESC, day ASC, user_id ASC",
                (prefix, today.date().isoformat(), snapshot_id),
            ).fetchall()
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
                          start_at=local_time(start_at).isoformat(), end_at=local_time(end_at).isoformat()))
    payload = dict(schema_version=1, board="historical-daily", snapshot_id=snapshot_id,
                   fetched_at=fetched_at, prefix=prefix, timezone="Asia/Shanghai",
                   cache_snapshot_id=cached_snapshot_id,
                   cache_through_date=through_date,
                   history_status="ready" if through_date >= today.date().isoformat() else "updating",
                   boundary_tolerance_minutes=30, user_count=len(users), record_count=len(users),
                   users=users)
    exporter._atomic_write(exporter.share_dir / "historical-daily.json",
                           json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    return payload


class DailyRecordWorker:
    """At most one child; background failures cannot prevent data publication."""
    def __init__(self, database_path, logger):
        self.database_path = Path(database_path)
        self.logger = logger
        self.process = None

    def start(self, prefix, fetched_at, snapshot_id):
        if self.process is not None:
            result = self.process.poll()
            if result is None:
                return
            if result:
                self.logger.warning("daily history worker exited with status %d; will resume", result)
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-B", "-m", "app.daily_records", "--database",
                 str(self.database_path.resolve()), "--prefix", prefix,
                 "--fetched-at", fetched_at, "--snapshot-id", str(snapshot_id)],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            self.process = None
            self.logger.warning("daily history worker could not start; will retry")

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def main():
    import argparse
    import os
    from .resource_guard import PressureGuard
    parser = argparse.ArgumentParser(description="Resume daily AC history cache; does not publish share data")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--fetched-at", required=True)
    parser.add_argument("--snapshot-id", type=int, required=True)
    args = parser.parse_args()
    import fcntl
    os.nice(10)
    lock_path = args.database.with_suffix(args.database.suffix + ".daily-records.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        update_daily_records(RankDatabase(args.database), args.prefix, args.fetched_at,
                             args.snapshot_id, pause=PressureGuard().check)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
