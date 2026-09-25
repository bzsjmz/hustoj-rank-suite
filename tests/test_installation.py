import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from app.resource_monitor import resource_log_directory
from scripts.render_systemd import UNITS, render_units

ROOT = Path(__file__).resolve().parents[1]


class InstallationTests(unittest.TestCase):
    def test_six_units_render_for_another_root_and_device(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / 'project'
            shutil.copytree(ROOT / 'systemd', project / 'systemd')
            output = Path(directory) / 'review'
            render_units(project, output, '/dev/nvme0n1')
            self.assertEqual(set(UNITS), {path.name for path in output.iterdir()})
            collector = (output / 'oj-rank.service').read_text()
            self.assertIn('IOReadBandwidthMax=/dev/nvme0n1 12M', collector)
            self.assertIn('IOWriteBandwidthMax=/dev/nvme0n1 6M', collector)
            monitor = (output / 'oj-rank-resource-monitor.service').read_text()
            self.assertIn(f'ReadWritePaths={project}/logs', monitor)
            self.assertIn('MemoryMax=96M', monitor)
            self.assertIn('Environment=PRE_RENDER_PAGES=3', (output / 'oj-rank-render.service').read_text())
            for path in output.iterdir():
                self.assertNotIn('__PROJECT_ROOT__', path.read_text())
                self.assertNotIn('/root/oj-rank', path.read_text())

    def test_shell_review_mode_never_invokes_service_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            shutil.copytree(ROOT / 'systemd', project / 'systemd')
            (project / 'scripts').mkdir()
            for name in ('install_systemd.sh', 'render_systemd.py'):
                shutil.copy2(ROOT / 'scripts' / name, project / 'scripts' / name)
            bindir = root / 'bin'
            bindir.mkdir()
            for name in ('systemctl', 'systemd-analyze', 'install'):
                tool = bindir / name
                tool.write_text('#!/bin/sh\nexit 91\n')
                tool.chmod(0o755)
            result = subprocess.run(['bash', str(project / 'scripts/install_systemd.sh'), '--io-device', '/dev/fixture',
                                     '--render-dir', str(root / 'review')], capture_output=True, text=True,
                                    env={**os.environ, 'PATH': str(bindir) + ':' + os.environ['PATH']})
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(6, len(list((root / 'review').glob('*.service'))))

    def test_monitor_log_directory_is_configurable(self):
        with patch.dict(os.environ, {'OJ_RESOURCE_LOG_DIR': '/tmp/fixture-logs'}):
            self.assertEqual(Path('/tmp/fixture-logs'), resource_log_directory())

    def test_healthcheck_reads_fixture_without_changing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            shutil.copy2(ROOT / 'scripts/healthcheck.sh', root / 'scripts/healthcheck.sh')
            (root / '.venv/bin').mkdir(parents=True)
            (root / '.venv/bin/python').symlink_to(sys.executable)
            db = root / 'rank.db'
            with sqlite3.connect(db) as connection:
                for name in ('current_rank', 'snapshots', 'student_roster'):
                    connection.execute(f'CREATE TABLE {name}(id INTEGER)')
            before = db.read_bytes()
            for name in ('systemctl', 'curl'):
                tool = root / '.venv/bin' / name
                tool.write_text('#!/bin/sh\nexit 0\n')
                tool.chmod(0o755)
            result = subprocess.run(['bash', str(root / 'scripts/healthcheck.sh')], capture_output=True, text=True,
                                    env={**os.environ, 'OJ_DATA_DIR': str(root),
                                         'PATH': str(root / '.venv/bin') + ':' + os.environ['PATH']})
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(before, db.read_bytes())
            self.assertIn('current_rank: 0', result.stdout)

    def test_unsafe_paths_are_rejected_before_writing_units(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                render_units(ROOT, Path(directory), '/dev/vda bad')
            self.assertEqual([], list(Path(directory).iterdir()))
