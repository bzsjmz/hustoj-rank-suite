"""Small /proc-only pressure checks; no database, credentials or network access."""
from __future__ import annotations
import json
import os
from pathlib import Path
import time

RUNTIME = Path('/run/oj-rank-resources')


def pressure_values(path: Path) -> dict:
    result = {}
    try:
        for line in path.read_text().splitlines():
            kind, *fields = line.split()
            result[kind] = {k: float(v) for k, v in (s.split('=') for s in fields)}
    except (OSError, ValueError):
        pass
    return result


def system_pressure() -> dict:
    try:
        mem = {line.split(':')[0]: int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines()}
    except (OSError, ValueError):
        mem = {}
    return {'available_kib': mem.get('MemAvailable'),
            'memory': pressure_values(Path('/proc/pressure/memory')),
            'io': pressure_values(Path('/proc/pressure/io'))}


class PressureGuard:
    """Hysteresis: stop early; require recovery before admitting new work."""
    def __init__(self):
        self.paused = False

    def check(self, sample=None):
        sample = system_pressure() if sample is None else sample
        available = sample.get('available_kib')
        memory = sample.get('memory', {}).get('some', {}).get('avg10', 0)
        io = sample.get('io', {}).get('full', {}).get('avg10', 0)
        if self.paused:
            self.paused = not (available is not None and available >= 768 * 1024 and memory < 2 and io < 5)
        else:
            self.paused = ((available is not None and available < 512 * 1024) or memory >= 10 or io >= 20)
        return self.paused


class RetryBackoff:
    def __init__(self, base=60, maximum=600):
        self.base = min(maximum, max(1, base))
        self.maximum = maximum
        self.failures = 0

    def failed(self):
        delay = min(self.maximum, self.base * 2 ** min(self.failures, 10))
        self.failures += 1
        return delay

    def reset(self):
        self.failures = 0


def write_status(name, **fields):
    """Bounded, atomic runtime status. Callers provide only non-sensitive fields."""
    try:
        RUNTIME.mkdir(mode=0o700, exist_ok=True)
        path = RUNTIME / (name + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'time': time.time(), 'pid': os.getpid(), **fields}))
        temporary.replace(path)
    except OSError:
        pass
