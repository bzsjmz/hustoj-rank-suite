"""Bounded local telemetry; intentionally excludes cmdline, environment and URLs."""
from __future__ import annotations
from datetime import datetime
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import signal
import threading
import time
from .resource_guard import PressureGuard, RUNTIME, system_pressure


def text(path):
    try:
        return Path(path).read_text()
    except OSError:
        return ''


def counters(path):
    result = {}
    for line in text(path).splitlines():
        parts = line.replace(':', '').split()
        if len(parts) >= 2:
            try:
                result[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return result


def io_counters(path):
    result = {}
    for line in text(path).splitlines():
        parts = line.split()
        if parts:
            result[parts[0]] = {k: int(v) for k, v in (p.split('=') for p in parts[1:])}
    return result


class Sampler:
    def __init__(self):
        self.previous = {}
        self.last = None
        self.last_pss = -1000.0

    def delta(self, key, current, next_previous):
        previous = self.previous.get(key)
        next_previous[key] = current
        return None if previous is None else {k: max(0, v - previous.get(k, v)) for k, v in current.items()}

    def sample(self):
        now = time.monotonic()
        previous = {}
        pressure = system_pressure()
        processes = []
        for proc in Path('/proc').glob('[0-9]*'):
            try:
                stat = (proc / 'stat').read_text()
                fields = stat[stat.rfind(')') + 2:].split()
                start = int(fields[19])
                status = counters(proc / 'status')
                io = counters(proc / 'io')
                identity = 'pid:' + proc.name + ':' + str(start)
                change = self.delta(identity, io, previous)
                processes.append({'pid': int(proc.name), 'start_ticks': start,
                    'comm': (proc / 'comm').read_text().strip()[:40],
                    'cgroup': text(proc / 'cgroup').strip().split('::')[-1],
                    'rss_kib': status.get('VmRSS', 0), 'anon_kib': status.get('RssAnon', 0),
                    'io': io, 'io_delta': change})
            except (OSError, ValueError, IndexError):
                continue
        top_memory = sorted(processes, key=lambda p: p['rss_kib'], reverse=True)[:20]
        top_io = sorted(processes, key=lambda p: sum((p['io_delta'] or {}).get(k, 0) for k in ('read_bytes', 'write_bytes')), reverse=True)[:20]
        selected = {p['pid']: p for p in top_memory + top_io}
        # smaps only for eight largest processes every five minutes, never under pressure.
        if now - self.last_pss >= 300 and (pressure['available_kib'] or 0) >= 768 * 1024:
            for row in top_memory[:8]:
                row['pss_kib'] = counters(Path('/proc') / str(row['pid']) / 'smaps_rollup').get('Pss')
            self.last_pss = now
        groups = []
        paths = list(Path('/sys/fs/cgroup/system.slice').glob('*'))
        paths.append(Path('/sys/fs/cgroup/user.slice'))
        for group in paths:
            raw = text(group / 'memory.current').strip()
            if not raw:
                continue
            io = io_counters(group / 'io.stat')
            flat = {device + ':' + key: value for device, values in io.items() for key, value in values.items()}
            stats = counters(group / 'memory.stat')
            try:
                inode = group.stat().st_ino
            except OSError:
                continue
            groups.append({'name': group.name, 'inode': inode, 'memory_bytes': int(raw),
                'memory': {k: v for k, v in stats.items() if k in ('anon', 'file', 'slab', 'pgscan', 'pgsteal', 'pgmajfault') or k.startswith('workingset_refault')},
                'events': counters(group / 'memory.events'), 'io': io,
                'io_delta': self.delta('cg:' + str(inode), flat, previous),
                'pressure': {kind: text(group / (kind + '.pressure')).strip() for kind in ('memory', 'io')}})
        vm = counters('/proc/vmstat')
        vm = {k: v for k, v in vm.items() if k.startswith(('pgscan', 'pgsteal', 'workingset_refault', 'pswp')) or k == 'pgmajfault'}
        services = {}
        for path in RUNTIME.glob('*.json'):
            try:
                services[path.stem] = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
        result = {'timestamp': datetime.now().astimezone().isoformat(),
            'boot_id': text('/proc/sys/kernel/random/boot_id').strip(),
            'interval_seconds': None if self.last is None else round(now - self.last, 3),
            'pressure': pressure, 'meminfo_kib': counters('/proc/meminfo'),
            'vmstat': vm, 'vmstat_delta': self.delta('vmstat', vm, previous),
            'diskstats': [l for l in text('/proc/diskstats').splitlines() if l.split()[2] == 'vda'],
            'process_count': len(processes), 'processes': list(selected.values()), 'cgroups': groups,
            'services': services}
        self.previous = previous
        self.last = now
        return result


def resource_log_directory() -> Path:
    return Path(os.environ.get("OJ_RESOURCE_LOG_DIR", str(Path(__file__).resolve().parents[1] / "logs")))


def run():
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log = logging.getLogger('resources')
    log.setLevel(logging.INFO)
    log_dir = resource_log_directory()
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / 'resources.jsonl', maxBytes=16 * 1024 * 1024, backupCount=15)
    handler.setFormatter(logging.Formatter('%(message)s'))
    log.addHandler(handler)
    sampler = Sampler()
    guard = PressureGuard()
    burst_until = 0.0
    was_paused = False
    while not stop.is_set():
        try:
            sample = sampler.sample()
            paused = guard.check(sample['pressure'])
            if paused and not was_paused:
                burst_until = time.monotonic() + 120
            was_paused = paused
            log.info(json.dumps(sample, separators=(',', ':'), ensure_ascii=False))
        except Exception as exc:
            # Never log exception messages, which may include paths or source contents.
            log.info(json.dumps({'timestamp': time.time(), 'error_type': type(exc).__name__}))
        stop.wait(10 if time.monotonic() < burst_until else 30)


if __name__ == '__main__':
    run()
