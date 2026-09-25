from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import time

from .config import Settings
from .database import RankDatabase
from .exporter import RankExporter
from .statistics_store import StatisticsStore


def backfill(settings: Settings, *, sleep_seconds: float = 0.2, limit: int = 0,
             start_id: int = 0, verify_existing: bool = False) -> dict[str, int]:
    if not math.isfinite(sleep_seconds) or sleep_seconds < 0 or limit < 0 or start_id < 0:
        raise ValueError('sleep, limit and start id must be finite and non-negative')
    database = RankDatabase(settings.database_path, read_only=True)
    exporter = RankExporter(settings.share_dir)
    store = StatisticsStore(settings.share_dir)
    # Capture a finite metadata boundary, then read the much smaller roster table.
    metadata = database.snapshot_metadata(settings.prefix)
    first_seen = database.roster_first_seen(settings.prefix)
    registered = store.index(settings.prefix)
    layout = settings.student_id_layout
    roster: set[str] = set()
    position = 0
    counts = {'published': 0, 'repaired': 0, 'existing': 0}
    examined = 0
    for snapshot_id, fetched_at, _ in metadata:
        while position < len(first_seen) and first_seen[position][0] <= snapshot_id:
            user_id = first_seen[position][1]
            if user_id not in settings.excluded_user_ids and layout.is_student_id(user_id):
                roster.add(user_id)
            position += 1
        if snapshot_id < start_id:
            continue
        examined += 1
        try:
            destination = store.snapshot_path(snapshot_id)
            indexed = registered.get(snapshot_id)
            if indexed is not None and indexed['fetched_at'] != fetched_at:
                raise ValueError('indexed timestamp conflicts with database')
            if destination.is_file():
                if indexed is not None and not verify_existing:
                    counts['existing'] += 1
                else:
                    if not store.repair_existing(settings.prefix, snapshot_id, fetched_at):
                        raise FileNotFoundError("snapshot disappeared during reconciliation")
                    counts['repaired' if indexed is None else 'existing'] += 1
            else:
                entries = database.snapshot_entries(snapshot_id, settings.prefix)
                exporter.export_statistics_snapshot(entries, settings.prefix, fetched_at,
                                                    snapshot_id, frozenset(roster))
                counts['published'] += 1
        finally:
            # Even the skip/repair/error paths yield I/O time to the collector.
            if sleep_seconds:
                time.sleep(sleep_seconds)
        if examined % 50 == 0:
            print(f'examined={examined}, snapshot_id={snapshot_id}, counts={counts}', flush=True)
        if limit and counts['published'] + counts['repaired'] >= limit:
            break
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description='Publish/reconcile OJ statistics history; this writes shared files.')
    parser.add_argument('--sleep-seconds', type=float, default=0.2,
                        help='pause after every examined snapshot, including skips (default: 0.2)')
    parser.add_argument('--limit', type=int, default=0,
                        help='maximum new publications plus repaired index entries (0 means all)')
    parser.add_argument('--start-id', type=int, default=0,
                        help='resume at this snapshot ID without losing the earlier roster')
    parser.add_argument('--verify-existing', action='store_true',
                        help='also decompress/validate indexed files; higher I/O cost')
    args = parser.parse_args()
    if not math.isfinite(args.sleep_seconds) or args.sleep_seconds < 0 or args.limit < 0 or args.start_id < 0:
        parser.error('sleep, limit and start id must be finite and non-negative')
    settings = Settings.from_env()
    os.nice(10)
    ionice = shutil.which('ionice')
    if ionice:
        result = subprocess.run([ionice, '-c', '3', '-p', str(os.getpid())], capture_output=True)
        if result.returncode:
            print('warning: idle I/O priority unavailable; per-snapshot throttling remains enabled')
    else:
        print('warning: ionice unavailable; per-snapshot throttling remains enabled')
    counts = backfill(settings, sleep_seconds=args.sleep_seconds, limit=args.limit,
                      start_id=args.start_id, verify_existing=args.verify_existing)
    print('statistics snapshot backfill complete: ' + ', '.join(f'{k}={v}' for k, v in counts.items()))
    return 0


if __name__ == '__main__':
    try:
        result = main()
    except KeyboardInterrupt:
        result = 130
    except Exception as exc:
        print(f'statistics backfill stopped: {type(exc).__name__}; previous files retained, retry after resolving the failure')
        result = 1
    raise SystemExit(result)
