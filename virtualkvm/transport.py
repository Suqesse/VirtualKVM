"""Serialized, nonblocking OpenSSL operations with a blocking socket facade.

An SSL object must not be entered concurrently by application reader/writer
threads. Waiting for network readiness must happen outside that serialization
lock, otherwise an idle reader would prevent the peer from receiving requests.
"""
from __future__ import annotations

import errno
import math
import select
import socket
import ssl
import threading
import time


class TLSConnection:
    """Own a handshaken SSLSocket and serialize its individual SSL operations."""

    def __init__(self, connection: ssl.SSLSocket, timeout: float | None = 10):
        self._raw = connection
        self._io_lock = threading.RLock()
        self._stopped = threading.Event()
        self._local = threading.local()
        self._default_timeout = self._validate_timeout(timeout)
        self._closed = False
        try:
            connection.setblocking(False)
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _validate_timeout(timeout):
        if timeout is None:
            return None
        timeout = float(timeout)
        if timeout < 0 or not math.isfinite(timeout):
            raise ValueError("Timeout must be a finite non-negative number or None")
        return timeout

    def settimeout(self, timeout):
        # SSLSocket.settimeout changes the underlying fd's mode globally. Keep
        # logical deadlines local to the caller; the raw socket stays nonblocking.
        self._local.timeout = self._validate_timeout(timeout)

    def gettimeout(self):
        return getattr(self._local, "timeout", self._default_timeout)

    def _deadline(self):
        timeout = self.gettimeout()
        return None if timeout is None else time.monotonic() + timeout

    def _ensure_open(self):
        if self._stopped.is_set():
            raise OSError(errno.EBADF, "TLS connection is closed")

    def _wait(self, want_read: bool, deadline, *, allow_pending: bool = False):
        while True:
            self._ensure_open()
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("TLS operation exceeded its deadline")
            # Another serialized SSL operation can have decrypted records into
            # OpenSSL's buffer without leaving the OS socket readable.
            if want_read and allow_pending:
                with self._io_lock:
                    self._ensure_open()
                    if self._raw.pending():
                        return
            interval = min(0.1, remaining) if remaining is not None else 0.1
            try:
                readable, writable, _ = select.select(
                    [self._raw] if want_read else [], [] if want_read else [self._raw], [], interval)
            except (OSError, ValueError) as error:
                self._ensure_open()
                raise OSError(errno.EIO, "TLS readiness wait failed") from error
            if readable or writable:
                return

    def recv(self, count: int, flags: int = 0) -> bytes:
        if flags:
            raise ValueError("Nonzero recv flags are not supported for TLS")
        if count < 0:
            raise ValueError("Receive length must be non-negative")
        deadline = self._deadline()
        while True:
            try:
                with self._io_lock:
                    self._ensure_open()
                    return self._raw.recv(count)
            except ssl.SSLWantReadError:
                self._wait(True, deadline, allow_pending=True)
            except ssl.SSLWantWriteError:
                self._wait(False, deadline)

    def sendall(self, data, flags: int = 0):
        if flags:
            raise ValueError("Nonzero send flags are not supported for TLS")
        # Keep the exact same view alive across SSL WANT retries. OpenSSL may
        # require a repeated write to use the same bytes until it makes progress.
        view = memoryview(data).cast("B")
        deadline = self._deadline()
        offset = 0
        while offset < len(view):
            pending = view[offset:]
            while True:
                try:
                    with self._io_lock:
                        self._ensure_open()
                        if deadline is not None and time.monotonic() >= deadline:
                            raise TimeoutError("TLS operation exceeded its deadline")
                        sent = self._raw.send(pending)
                    if sent <= 0:
                        raise OSError(errno.ECONNRESET, "TLS peer disconnected while sending")
                    offset += sent
                    break
                except ssl.SSLWantReadError:
                    self._wait(True, deadline)
                except ssl.SSLWantWriteError:
                    self._wait(False, deadline)

    def fileno(self):
        with self._io_lock:
            return self._raw.fileno()

    def shutdown(self, how=socket.SHUT_RDWR):
        self._stopped.set()
        with self._io_lock:
            if not self._closed:
                self._raw.shutdown(how)

    def close(self):
        self._stopped.set()
        with self._io_lock:
            if not self._closed:
                self._closed = True
                self._raw.close()
