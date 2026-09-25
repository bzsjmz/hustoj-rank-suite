"""Run backend tests with CI annotations and a spawn-safe entry point."""
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    for test, traceback in result.failures + result.errors:
        message = (str(test) + '\n' + traceback).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print('::error::' + message, flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
