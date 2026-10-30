"""Nonblocking TLS facade serialization, readiness and timeout behavior."""
import socket
import ssl
import threading
import time
import unittest

from virtualkvm.transport import TLSConnection


class RejectConcurrentSSL:
    """Fail deterministically if an SSL operation enters during another one."""
    def __init__(self):
        self.active = threading.Lock()
        self.operations = []
        self.nonblocking = False

    def setblocking(self, value):
        self.nonblocking = not value

    def _operate(self, kind, result):
        if not self.active.acquire(blocking=False):
            raise AssertionError("Concurrent SSL operations are unsafe")
        try:
            time.sleep(0.001)  # Release the GIL so unguarded calls overlap.
            self.operations.append(kind)
            return result
        finally:
            self.active.release()

    def recv(self, count):
        return self._operate("recv", b"x" * count)

    def send(self, data):
        return self._operate("send", len(data))

    def close(self):
        self._operate("close", None)

    def shutdown(self, _):
        self._operate("shutdown", None)


class WantSocket:
    """Use real readiness while exposing nonblocking SSL WANT exceptions."""
    def __init__(self, connection):
        self.connection = connection
        self.force_want_write = True

    def setblocking(self, value):
        self.connection.setblocking(value)

    def recv(self, count):
        try:
            return self.connection.recv(count)
        except BlockingIOError:
            raise ssl.SSLWantReadError() from None

    def send(self, data):
        if self.force_want_write:
            self.force_want_write = False
            raise ssl.SSLWantWriteError()
        try:
            return self.connection.send(data[:3])  # Exercise partial writes.
        except BlockingIOError:
            raise ssl.SSLWantWriteError() from None

    def fileno(self):
        return self.connection.fileno()

    def pending(self):
        return 0

    def shutdown(self, how):
        self.connection.shutdown(how)

    def close(self):
        self.connection.close()


class TransportTests(unittest.TestCase):
    def test_ssl_calls_are_serialized_between_reader_writer_and_close(self):
        raw = RejectConcurrentSSL()
        connection = TLSConnection(raw)
        failures = []
        start = threading.Barrier(3)
        def read():
            try:
                start.wait()
                for _ in range(100):
                    self.assertEqual(connection.recv(1), b"x")
            except Exception as error:
                failures.append(error)
        def write():
            try:
                start.wait()
                for _ in range(100):
                    connection.sendall(b"x")
            except Exception as error:
                failures.append(error)
        reader, writer = threading.Thread(target=read), threading.Thread(target=write)
        reader.start(); writer.start(); start.wait()
        reader.join(timeout=3); writer.join(timeout=3)
        connection.shutdown()
        connection.close()
        self.assertFalse(reader.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(raw.nonblocking)
        self.assertEqual(raw.operations.count("recv"), 100)
        self.assertEqual(raw.operations.count("send"), 100)

    def test_timeouts_are_thread_local_and_other_threads_keep_protocol_default(self):
        connection = TLSConnection(RejectConcurrentSSL())
        connection.settimeout(0.25)
        observed = []
        barrier = threading.Barrier(3)
        def inspect(value):
            observed.append(("default", connection.gettimeout()))
            connection.settimeout(value)
            barrier.wait()
            observed.append(("local", connection.gettimeout()))
        first, second = threading.Thread(target=inspect, args=(1,)), threading.Thread(target=inspect, args=(2,))
        first.start(); second.start(); barrier.wait()
        first.join(timeout=1); second.join(timeout=1)
        self.assertEqual(sorted(value for kind, value in observed if kind == "default"), [10, 10])
        self.assertEqual(sorted(value for kind, value in observed if kind == "local"), [1, 2])
        self.assertEqual(connection.gettimeout(), 0.25)
        connection.close()

    def test_want_read_wait_does_not_hold_writer_lock_and_want_write_retries(self):
        local, peer = socket.socketpair()
        connection = TLSConnection(WantSocket(local), timeout=1)
        received, failures = [], []
        def read():
            try:
                received.append(connection.recv(4))
            except Exception as error:
                failures.append(error)
        reader = threading.Thread(target=read)
        try:
            reader.start()
            connection.sendall(b"ping")
            peer.settimeout(1)
            data = bytearray()
            while len(data) < 4:
                data.extend(peer.recv(4 - len(data)))
            self.assertEqual(bytes(data), b"ping")
            peer.sendall(b"pong")
            reader.join(timeout=1)
            self.assertFalse(reader.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(received, [b"pong"])
        finally:
            connection.close(); peer.close(); reader.join(timeout=1)

    def test_read_deadline_and_close_interrupt_an_idle_wait(self):
        local, peer = socket.socketpair()
        connection = TLSConnection(WantSocket(local), timeout=0.05)
        try:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                connection.recv(1)
            self.assertLess(time.monotonic() - started, 0.5)
            failures = []
            def wait():
                connection.settimeout(None)
                try:
                    connection.recv(1)
                except OSError as error:
                    failures.append(error)
            reader = threading.Thread(target=wait)
            reader.start()
            started = time.monotonic()
            connection.close()
            reader.join(timeout=0.5)
            self.assertFalse(reader.is_alive())
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(len(failures), 1)
        finally:
            connection.close(); peer.close()

    def test_partial_write_progress_does_not_extend_the_overall_deadline(self):
        class SlowPartialSSL(RejectConcurrentSSL):
            def send(self, _):
                time.sleep(0.005)
                return self._operate("send", 1)
        raw = SlowPartialSSL()
        connection = TLSConnection(raw, timeout=0.02)
        try:
            with self.assertRaises(TimeoutError):
                connection.sendall(b"x" * 50)
            self.assertLess(raw.operations.count("send"), 50)
        finally:
            connection.close()

    def test_write_want_read_does_not_spin_on_buffered_plaintext(self):
        class PendingButWantRead(WantSocket):
            calls = 0
            def send(self, _):
                self.calls += 1
                raise ssl.SSLWantReadError()
            def pending(self):
                return 1
        local, peer = socket.socketpair()
        raw = PendingButWantRead(local)
        connection = TLSConnection(raw, timeout=0.03)
        try:
            with self.assertRaises(TimeoutError):
                connection.sendall(b"x")
            self.assertEqual(raw.calls, 1)
        finally:
            connection.close(); peer.close()

    def test_receive_want_read_can_retry_buffered_plaintext_without_network_data(self):
        class PendingRead(WantSocket):
            calls = 0
            def recv(self, _):
                self.calls += 1
                if self.calls == 1:
                    raise ssl.SSLWantReadError()
                return b"buffered"
            def pending(self):
                return 8
        local, peer = socket.socketpair()
        connection = TLSConnection(PendingRead(local), timeout=0.03)
        try:
            self.assertEqual(connection.recv(8), b"buffered")
        finally:
            connection.close(); peer.close()


if __name__ == "__main__":
    unittest.main()
