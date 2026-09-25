#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
database="${OJ_DATA_DIR:-${project_root}/data}/rank.db"
session_status="${OJ_DATA_DIR:-${project_root}/data}/webvpn-session-status.json"

systemctl show oj-rank.service oj-rank-render.service --property=Id,ActiveState,SubState,MainPID
systemctl is-active --quiet oj-rank.service
systemctl is-active --quiet oj-rank-render.service
curl --fail --silent --show-error --output /dev/null http://127.0.0.1:6080/vnc.html

if [[ -f "${database}" ]]; then
    "${project_root}/.venv/bin/python" -B -c \
        'import sqlite3,sys; from pathlib import Path; c=sqlite3.connect(Path(sys.argv[1]).resolve().as_uri()+"?mode=ro", uri=True); c.execute("PRAGMA query_only=ON"); print("current_rank:", c.execute("select count(*) from current_rank").fetchone()[0]); print("snapshots:", c.execute("select count(*) from snapshots").fetchone()[0]); print("student_roster:", c.execute("select count(*) from student_roster").fetchone()[0])' \
        "${database}"
else
    echo "database not created yet: ${database}"
fi

if [[ -f "${session_status}" ]]; then
    "${project_root}/.venv/bin/python" -B -c \
        'import json,sys; s=json.load(open(sys.argv[1])); print("webvpn_state:", s.get("state", "UNKNOWN")); print("authenticated_at:", s.get("authenticated_at", "-")); print("last_session_age_seconds:", s.get("last_session_age_seconds", "-")); print("last_http_status:", s.get("last_http_status", "-")); print("last_classification:", s.get("last_classification", "-"))' \
        "${session_status}"
else
    echo "session diagnostics not created yet: ${session_status}"
fi
