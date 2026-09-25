#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
io_device=""
render_dir=""
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --io-device|--render-dir)
            if [[ "$#" -lt 2 ]]; then echo "Missing value for $1" >&2; exit 2; fi
            if [[ "$1" == "--io-device" ]]; then io_device="$2"; else render_dir="$2"; fi
            shift 2 ;;
        -h|--help)
            echo "Usage: $0 --io-device /dev/DEVICE [--render-dir DIRECTORY]"
            echo "--render-dir only writes reviewable templates; no root or systemctl required."
            exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done
if [[ -z "${io_device}" ]]; then echo "Specify the backing block device with --io-device; no device is assumed." >&2; exit 2; fi
if [[ -n "${render_dir}" ]]; then
    python3 -B "${project_root}/scripts/render_systemd.py" --project-root "${project_root}" \
        --io-device "${io_device}" --output-dir "${render_dir}"
    echo "Rendered units for review in ${render_dir}; no services changed."
    exit 0
fi
if [[ "${EUID}" -ne 0 ]]; then echo "Installation requires root; use --render-dir for review." >&2; exit 1; fi
if [[ ! -b "${io_device}" ]]; then echo "Not a block device: ${io_device}" >&2; exit 2; fi
staging_dir="$(mktemp -d)"
trap 'rm -rf -- "${staging_dir}"' EXIT
python3 -B "${project_root}/scripts/render_systemd.py" --project-root "${project_root}" \
    --io-device "${io_device}" --output-dir "${staging_dir}"
if [[ ! -d "${project_root}/logs" ]]; then install -d -m 0750 "${project_root}/logs"; fi
systemd-analyze verify "${staging_dir}"/*.service
for unit in "${staging_dir}"/*.service; do
    install -m 0644 "${unit}" "/etc/systemd/system/$(basename "${unit}")"
done
systemctl daemon-reload
systemctl enable oj-rank.service oj-rank-render.service oj-rank-resource-monitor.service
echo "Installed six units and enabled collector, renderer and resource monitor. No service was started or restarted."
