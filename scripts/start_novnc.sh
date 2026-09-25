#!/usr/bin/env bash
set -euo pipefail

display="${DISPLAY:-:99}"
vnc_port="${VNC_PORT:-5900}"
novnc_port="${NOVNC_PORT:-6080}"

for _ in $(seq 1 50); do
    if DISPLAY="${display}" xdpyinfo >/dev/null 2>&1; then
        break
    fi
    sleep 0.2
done
DISPLAY="${display}" xdpyinfo >/dev/null

x11vnc \
    -display "${display}" \
    -localhost \
    -rfbport "${vnc_port}" \
    -forever \
    -shared \
    -nopw \
    -noxdamage \
    -quiet &

exec websockify \
    --web=/usr/share/novnc \
    "127.0.0.1:${novnc_port}" \
    "127.0.0.1:${vnc_port}"
