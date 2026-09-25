from __future__ import annotations

import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    str(PROJECT_ROOT / "data" / "ms-playwright"),
)

from playwright.sync_api import sync_playwright

from app.models import RankEntry
from app.renderer import LeaderboardRenderer


def main() -> None:
    share_dir = PROJECT_ROOT / "share"
    payload = json.loads((share_dir / "latest.json").read_text(encoding="utf-8"))
    users = payload.get("users")
    if not isinstance(users, list) or not users:
        raise RuntimeError("latest.json does not contain any users")
    if payload.get("user_count") != len(users):
        raise RuntimeError("latest.json user count mismatch")

    entries = [
        RankEntry(
            rank=int(row["rank"]),
            user_id=str(row["user_id"]),
            nickname=str(row.get("nickname") or ""),
            accepted=int(row["accepted"]),
            submitted=int(row["submitted"]),
            ratio=float(row["ratio"]),
            level=str(row.get("level") or ""),
        )
        for row in users
    ]
    browser_candidates = sorted(
        (PROJECT_ROOT / "data" / "ms-playwright").glob(
            "chromium-*/chrome-linux/chrome"
        )
    )
    if not browser_candidates:
        raise RuntimeError("Playwright Chromium executable was not found")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(browser_candidates[-1]),
            headless=True,
            args=["--no-sandbox"],
        )
        context = browser.new_context()
        try:
            manifest = LeaderboardRenderer(share_dir).render(
                context,
                entries,
                str(payload.get("prefix", "")),
                str(payload.get("fetched_at", "")),
                int(payload["snapshot_id"]),
            )
        finally:
            context.close()
            browser.close()

    print(
        f"rendered snapshot {manifest['snapshot_id']}: "
        f"{manifest['page_count']} pages, {manifest['user_count']} users"
    )


if __name__ == "__main__":
    main()
