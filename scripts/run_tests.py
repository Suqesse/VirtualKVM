"""Run the real test suite with periodic deadlock diagnostics for native CI."""
from __future__ import annotations

import faulthandler
from pathlib import Path
import sys
import unittest


def main():
    root = Path(__file__).resolve().parents[1]
    diagnostic_path = root / "build/validation/thread-diagnostics.log"
    diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
    diagnostics = diagnostic_path.open("w", encoding="utf-8")
    faulthandler.enable(file=diagnostics)
    faulthandler.dump_traceback_later(60, repeat=True, file=diagnostics)
    try:
        sys.path.insert(0, str(root))
        suite = unittest.defaultTestLoader.discover(str(root / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.testsRun > 0 and result.wasSuccessful() else 1
    finally:
        faulthandler.cancel_dump_traceback_later()
        faulthandler.disable()
        diagnostics.close()


if __name__ == "__main__":
    raise SystemExit(main())
