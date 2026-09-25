from __future__ import annotations

import sqlite3

from app.config import Settings
from app.database import RankDatabase


def main() -> None:
    settings = Settings.from_env()
    database = RankDatabase(settings.database_path)
    database.initialize()
    with sqlite3.connect(settings.database_path) as connection:
        roster_count = int(
            connection.execute("SELECT COUNT(*) FROM student_roster").fetchone()[0]
        )
        snapshot_count = int(
            connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        )
    visible_count = len(
        database.historical_roster_user_ids(
            settings.prefix,
            settings.excluded_user_ids,
            student_id_layout=settings.student_id_layout,
        )
    )
    print(
        f"roster_rows={roster_count} visible_students={visible_count} "
        f"snapshots={snapshot_count}"
    )


if __name__ == "__main__":
    main()
