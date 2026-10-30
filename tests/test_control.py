"""Real loopback TLS tests; no native input or external network access."""
import base64
import hashlib
from pathlib import Path
import queue
import socket
import struct
import tempfile
import threading
import time
import unittest

from virtualkvm.control import (HostServer, ViewerClient, ProtocolError, MAX_MESSAGE,
    _receive_message, _send_message)
from virtualkvm.security import (SecurityError, connect_pinned, normalize_fingerprint,
    pairing_proof, server_identity)


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Condition was not observed before its deadline")


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.events = queue.Queue()
        self.clipboard = queue.Queue()
        self.reverse_clipboard = queue.Queue()
        self.disconnected = threading.Event()
        self.host = HostServer(bind="127.0.0.1", port=0,
            state_dir=self.root / "host", pin="123456789012", on_event=self.events.put,
            on_clipboard=self.clipboard.put, on_disconnect=self.disconnected.set,
            receive_dir=self.root / "received",
            displays=[{"index": 0, "name": "Synthetic", "width": 640, "height": 360}])
        self.host.start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.disconnect()
        self.host.stop()
        self.temporary.cleanup()

    def client(self, pin="123456789012", fingerprint=None):
        client = ViewerClient(self.root / "viewer", on_clipboard=self.reverse_clipboard.put)
        self.clients.append(client)
        client.connect(*self.host.address, pin=pin,
                       fingerprint=fingerprint or self.host.fingerprint)
        return client

    def test_pinned_tls_authentication_and_async_input(self):
        client = self.client()
        self.assertEqual(client.displays[0]["name"], "Synthetic")
        event = {"type": "key", "key": "a", "down": True}
        client.send_event(event)
        self.assertEqual(self.events.get(timeout=2), event)
        self.assertTrue(self.host.sessions)
        self.assertTrue(client.connected)

    def test_wrong_pin_and_certificate_are_refused(self):
        with self.assertRaises(SecurityError):
            self.client(pin="000000000000")
        with self.assertRaises(SecurityError):
            self.client(fingerprint="00" * 32)
        client = ViewerClient(self.root / "viewer")
        with self.assertRaises(SecurityError):
            client.connect(*self.host.address, pin="123456789012")
        self.assertFalse(self.host.sessions)
        self.assertTrue(self.events.empty())

    def test_identity_persists_and_pin_is_not_written(self):
        _, fingerprint = server_identity(self.root / "host")
        self.assertEqual(fingerprint, self.host.fingerprint)
        self.assertEqual(len(normalize_fingerprint(fingerprint)), 64)
        for path in (self.root / "host").iterdir():
            self.assertNotIn(b"123456789012", path.read_bytes())
            if __import__("os").name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_clipboard_both_directions(self):
        client = self.client()
        client.send_clipboard("Text from viewer — текст")
        self.assertEqual(self.clipboard.get(timeout=2), "Text from viewer — текст")
        self.host.broadcast_clipboard("Text from host")
        self.assertEqual(self.reverse_clipboard.get(timeout=2), "Text from host")
        with self.assertRaises(ProtocolError):
            client.send_clipboard("x" * (256 * 1024 + 1))

    def test_file_transfer_integrity_and_no_overwrite(self):
        client = self.client()
        source = self.root / "example.bin"
        payload = bytes(range(256)) * 1025
        source.write_bytes(payload)
        result = client.send_file(source)
        self.assertEqual((self.root / "received" / "example.bin").read_bytes(), payload)
        self.assertEqual(result["sha256"], hashlib.sha256(payload).hexdigest())
        with self.assertRaises(ProtocolError):
            client.send_file(source)
        self.assertEqual((self.root / "received" / "example.bin").read_bytes(), payload)
        self.assertFalse(list((self.root / "received").glob("*.part")))

    def test_empty_file_and_rejected_traversal_checksum(self):
        client = self.client()
        empty = self.root / "empty.txt"
        empty.touch()
        client.send_file(empty)
        self.assertEqual((self.root / "received" / "empty.txt").stat().st_size, 0)
        for name in ("../escape", "..\\escape", "CON", "trailing.", "bad:name", ""):
            with self.assertRaises(ProtocolError):
                client._request("file_begin", name=name, size=2)
        client._request("file_begin", name="tampered", size=2)
        client._request("file_chunk", data=base64.b64encode(b"ab").decode())
        with self.assertRaises(ProtocolError):
            client._request("file_end", sha256="00" * 32)
        self.assertFalse((self.root / "received" / "tampered").exists())
        self.assertFalse(list((self.root / "received").glob("*.part")))

    def test_disconnect_aborts_partial_transfer_and_releases_keys(self):
        client = self.client()
        client._request("file_begin", name="partial", size=20)
        client._request("file_chunk", data=base64.b64encode(b"abc").decode())
        client.disconnect()
        self.assertTrue(self.disconnected.wait(2))
        self.assertFalse(list((self.root / "received").iterdir()))
        self.assertFalse(self.host.sessions)

    def test_single_viewer_reconnect_and_server_shutdown(self):
        first = self.client()
        with self.assertRaises(SecurityError):
            self.client()
        first.disconnect()
        wait_for(lambda: not self.host.sessions)
        second = self.client()
        self.host.stop()
        wait_for(lambda: not second.connected)
        with self.assertRaises(ProtocolError):
            second.send_event({"type": "release_all"})

    def test_stream_negotiation_peer_is_taken_from_socket(self):
        requests = []
        started = threading.Event()
        self.host.on_stream_request = lambda request: requests.append(request) or {"streams": [{"display": 0, "port": 24900}]}
        self.host.on_stream_start = started.set
        client = self.client()
        response = client.request_stream([0], {"fps": 240, "receiver_ports": {"0": 24900}})
        self.assertEqual(response["streams"][0]["port"], 24900)
        self.assertEqual(requests[0]["_peer_host"], "127.0.0.1")
        self.assertEqual(requests[0]["config"]["fps"], 240)
        client.start_streams()
        self.assertTrue(started.is_set())
        with self.assertRaises(ProtocolError):
            client.request_stream([0, 0], {})

    def test_oversized_frame_disconnects_without_callback(self):
        connection = connect_pinned(*self.host.address, self.host.fingerprint)
        challenge = _receive_message(connection)
        _send_message(connection, {"type": "authenticate", "proof": pairing_proof(
            "123456789012", challenge["nonce"], self.host.fingerprint)}, threading.Lock())
        self.assertEqual(_receive_message(connection)["type"], "authenticated")
        connection.sendall(struct.pack("!I", MAX_MESSAGE + 1))
        self.assertTrue(self.disconnected.wait(2))
        self.assertTrue(self.events.empty())
        connection.close()

    def test_pairing_rate_limit(self):
        for _ in range(5):
            with self.assertRaises(SecurityError):
                self.client(pin="000000000000")
        with self.assertRaises((EOFError, ProtocolError)):
            self.client()

    def test_heartbeat_keeps_idle_connection_healthy(self):
        client = self.client()
        # Verify heartbeat's RPC path directly without sleeping ten seconds.
        self.assertIn("time", client._request("ping", timeout=2))

    def test_stop_interrupts_all_pending_tls_handshakes(self):
        sockets = []
        try:
            for _ in range(8):
                sockets.append(socket.create_connection(self.host.address, timeout=2))
            wait_for(lambda: len(self.host._connections) == 8)
            threads = list(self.host._threads)
            start = time.monotonic()
            self.host.stop()
            self.assertLess(time.monotonic() - start, 2)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        finally:
            for connection in sockets:
                connection.close()

    def test_partial_frame_has_an_absolute_deadline(self):
        sender, receiver = socket.socketpair()
        def trickle():
            try:
                for byte in struct.pack("!I", 100) + b"x" * 100:
                    sender.sendall(bytes([byte]))
                    time.sleep(0.04)
            except OSError:
                pass
        worker = threading.Thread(target=trickle, daemon=True)
        try:
            worker.start()
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                _receive_message(receiver, timeout=0.15)
            self.assertLess(time.monotonic() - start, 0.35)
        finally:
            sender.close()
            receiver.close()
            worker.join(timeout=1)

    def test_remote_status_and_callback_errors_are_visible(self):
        statuses = queue.Queue()
        client = self.client()
        client.on_status = statuses.put
        self.host.broadcast_status("Video failed: encoder unavailable")
        self.assertEqual(statuses.get(timeout=2), "Video failed: encoder unavailable")
        def reject(_):
            raise ValueError("Input permission denied")
        self.host.on_event = reject
        client.send_event({"type": "key", "key": "a", "down": True})
        self.assertIn("Input permission denied", statuses.get(timeout=2))
        self.host.on_stream_request = reject
        with self.assertRaisesRegex(ProtocolError, "Input permission denied"):
            client.request_stream([0], {})

    @unittest.skipIf(__import__("os").name == "nt", "Test creation of a dangling symlink requires Unix")
    def test_dangling_destination_symlink_is_not_replaced(self):
        client = self.client()
        directory = self.root / "received"
        directory.mkdir()
        destination = directory / "target"
        destination.symlink_to(self.root / "outside")
        client._request("file_begin", name="target", size=0)
        with self.assertRaises(ProtocolError):
            client._request("file_end", sha256=hashlib.sha256(b"").hexdigest())
        self.assertTrue(destination.is_symlink())
        self.assertFalse((self.root / "outside").exists())
        self.assertFalse(list(directory.glob("*.part")))


if __name__ == "__main__":
    unittest.main()
