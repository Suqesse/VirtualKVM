"""Run the real test suite with periodic deadlock diagnostics for native CI."""
from __future__ import annotations

import faulthandler
from pathlib import Path
import sys
import unittest


def main():
    faulthandler.enable()
    faulthandler.dump_traceback_later(60, repeat=True)
    try:
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root))
        suite = unittest.defaultTestLoader.discover(str(root / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.testsRun > 0 and result.wasSuccessful() else 1
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    raise SystemExit(main())
