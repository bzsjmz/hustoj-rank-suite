"""Render service templates without installing, enabling or starting services."""
from __future__ import annotations

import argparse
from pathlib import Path
import re

UNITS = (
    'oj-rank-display.service', 'oj-rank-window-manager.service',
    'oj-rank-novnc.service', 'oj-rank.service', 'oj-rank-render.service',
    'oj-rank-resource-monitor.service',
)


def render_units(project_root: Path, output_dir: Path, io_device: str) -> None:
    project_root = project_root.resolve()
    # Literal systemd substitutions: reject whitespace/specifiers rather than
    # accidentally interpreting them as extra arguments or unit specifiers.
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(project_root)):
        raise ValueError('project path must contain only ASCII letters, digits, /, _, ., -')
    if not re.fullmatch(r'/dev/[A-Za-z0-9_./-]+', io_device) or '..' in Path(io_device).parts:
        raise ValueError('io device must be an explicit safe /dev path')
    output_dir = output_dir.resolve()
    if output_dir in (project_root / 'systemd', Path('/etc/systemd/system'), Path('/usr/lib/systemd/system')):
        raise ValueError('render into a separate review directory, not an installed unit directory')
    rendered = {}
    for unit in UNITS:
        template = (project_root / 'systemd' / unit).read_text()
        content = template.replace('__PROJECT_ROOT__', str(project_root)).replace('__IO_DEVICE__', io_device)
        if re.search(r'__[A-Z_]+__', content):
            raise ValueError('unresolved template placeholder: ' + unit)
        rendered[unit] = content
    output_dir.mkdir(parents=True, exist_ok=True)
    for unit, content in rendered.items():
        path = output_dir / unit
        path.write_text(content)
        path.chmod(0o644)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--io-device', required=True)
    args = parser.parse_args()
    render_units(args.project_root, args.output_dir, args.io_device)


if __name__ == '__main__':
    main()
