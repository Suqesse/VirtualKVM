"""Dedicated pinned-TLS video connections; FFmpeg only opens loopback sockets."""
from __future__ import annotations

import socket
import threading

from .security import SecurityError


def close_socket(connection):
    if connection is None:
        return
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except (OSError, ValueError):
        pass
    try:
        connection.close()
    except OSError:
        pass


class OutputSlot:
    def __init__(self, key):
        self.key = key
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._connection = None
        self._sender = None
        self._claimed = False

    def claim(self, connection):
        with self._lock:
            if self._claimed or self._closed.is_set():
                raise SecurityError('Media ticket is no longer available')
            self._claimed = True
            self._connection = connection

    def start(self, sender):
        self._sender = sender
        self._ready.set()

    def forward(self):
        if not self._ready.wait(20) or self._closed.is_set():
            return
        process = self._sender._process
        if process is None or process.stdout is None:
            return
        self._connection.settimeout(15)
        read = getattr(process.stdout, 'read1', process.stdout.read)
        try:
            while not self._closed.is_set():
                block = read(64 * 1024)
                if not block:
                    return
                self._connection.sendall(block)
        except (OSError, ValueError):
            pass
        finally:
            self.close()

    def close(self):
        self._closed.set()
        self._ready.set()
        close_socket(self._connection)


class InputBridge:
    """TLS MPEG-TS -> one local FFmpeg TCP reader, without an inbound LAN port."""
    def __init__(self, connection):
        self.connection = connection
        self._stopped = threading.Event()
        self.last_error = None
        self._local = None
        self._listener = socket.socket()
        self._listener.bind(('127.0.0.1', 0))
        self._listener.listen(1)
        self._listener.settimeout(15)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._forward, name='tls-video-input', daemon=True)
        self._thread.start()

    def _forward(self):
        try:
            local, _ = self._listener.accept()
            self._local = local
            local.settimeout(15)
            self.connection.settimeout(20)
            while not self._stopped.is_set():
                data = self.connection.recv(64 * 1024)
                if not data:
                    raise EOFError('The host ended the video stream')
                local.sendall(data)
        except (OSError, EOFError, ValueError) as error:
            if not self._stopped.is_set():
                self.last_error = f'Защищённый видеопоток прерван: {type(error).__name__}'
        finally:
            self.stop()

    def stop(self):
        self._stopped.set()
        close_socket(self.connection)
        close_socket(self._local)
        close_socket(self._listener)
