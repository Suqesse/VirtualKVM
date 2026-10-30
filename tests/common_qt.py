"""Qt test scheduling with the same GIL behavior as QApplication.exec."""
from PySide6.QtCore import QEventLoop, QTimer


def wait_ms(milliseconds: int) -> None:
    """Process events while allowing Python callbacks in Qt worker threads.

    PySide6 6.12's QTest.qWait retains the GIL while processing events. A
    deferred multimedia deletion can then wait for an audio worker acquiring
    that same GIL for its Shiboken QObject cleanup. QEventLoop.exec releases
    it, matching the application's normal event loop instead of deadlocking
    the test harness during a valid multimedia teardown.
    """
    loop = QEventLoop()
    QTimer.singleShot(milliseconds, loop.quit)
    loop.exec()
