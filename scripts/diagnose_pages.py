#!/usr/bin/env python3
"""Read-only pagination diagnostic using the configured persistent profile."""

from __future__ import annotations

import hashlib
import os

from playwright.sync_api import sync_playwright

from app.config import Settings
from app.parser import is_webvpn_login_page, parse_ranklist
from app.session import sanitize_url


def main() -> int:
    settings = Settings.from_env()
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str(settings.playwright_browsers_path)
    )
    seen: set[str] = set()
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(settings.profile_dir),
            headless=False,
            viewport={"width": 1365, "height": 768},
            args=["--disable-dev-shm-usage"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        for page_index in range(3):
            requested_url = settings.ranklist_url(page_index * settings.page_size)
            page.goto(requested_url, wait_until="domcontentloaded", timeout=settings.page_timeout_ms)
            html = page.content()
            if is_webvpn_login_page(page.url, html):
                print(f"page={page_index} result=NEED_LOGIN final_url={sanitize_url(page.url)}")
                return 2
            entries = parse_ranklist(html)
            user_ids = [entry.user_id for entry in entries]
            overlap = sorted(seen.intersection(user_ids))
            digest = hashlib.sha256(html.encode("utf-8")).hexdigest()[:12]
            first = f"{entries[0].rank}:{entries[0].user_id}" if entries else "-"
            last = f"{entries[-1].rank}:{entries[-1].user_id}" if entries else "-"
            print(
                f"page={page_index} count={len(entries)} first={first} last={last} "
                f"overlap={overlap} digest={digest}"
            )
            print(f"requested_url={sanitize_url(requested_url)}")
            print(f"final_url={sanitize_url(page.url)}")
            seen.update(user_ids)
        context.close()
    return 0


if __name__ == "__main__":
    try:
        result = main()
    except Exception as exc:
        print(f"diagnostic failed: {type(exc).__name__}")
        result = 1
    raise SystemExit(result)
