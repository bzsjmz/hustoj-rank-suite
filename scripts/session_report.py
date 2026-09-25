from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("OJ_DATA_DIR", str(PROJECT_ROOT / "data"))).expanduser()
STATUS_PATH = DATA_DIR / "webvpn-session-status.json"
EVENTS_PATH = DATA_DIR / "webvpn-session-events.jsonl"


def duration(value) -> str:
    if value is None:
        return "-"
    return str(timedelta(seconds=int(value)))


def main() -> None:
    if not STATUS_PATH.exists():
        print("WebVPN session diagnostics have not been generated yet.")
        return
    status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    print(f"state: {status.get('state', 'UNKNOWN')}")
    print(f"reason: {status.get('state_reason', '-')}")
    print(f"authenticated_at: {status.get('authenticated_at', '-')}")
    print(f"last_success_at: {status.get('last_success_at', '-')}")
    print(f"last_failure_at: {status.get('last_failure_at', '-')}")
    print(f"last_session_age: {duration(status.get('last_session_age_seconds'))}")
    print(f"last_http_status: {status.get('last_http_status', '-')}")
    print(f"last_classification: {status.get('last_classification', '-')}")
    print(f"last_final_url: {status.get('last_final_url', '-')}")
    print(f"last_redirect_count: {status.get('last_redirect_count', 0)}")
    print("cookies:")
    for cookie in status.get("cookies", []):
        lifetime = (
            "session"
            if cookie.get("session_cookie")
            else cookie.get("expires_at", "-")
        )
        print(
            f"  {cookie.get('name')} domain={cookie.get('domain')} "
            f"lifetime={lifetime} fp={cookie.get('value_fingerprint')}"
        )

    if not EVENTS_PATH.exists():
        return
    lifetimes = []
    for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            event.get("event") == "state_transition"
            and event.get("state") == "RECOVERING"
            and event.get("session_age_seconds") is not None
        ):
            lifetimes.append(event)
    if lifetimes:
        print("observed_session_failures:")
        for event in lifetimes[-10:]:
            print(
                f"  {event.get('timestamp')} age="
                f"{duration(event.get('session_age_seconds'))} "
                f"reason={event.get('reason')}"
            )


if __name__ == "__main__":
    main()
