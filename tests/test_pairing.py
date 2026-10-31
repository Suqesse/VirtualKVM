"""Actual TLS quick pairing, per-session permissions and ticket isolation."""
from dataclasses import asdict
from pathlib import Path
import tempfile
import threading
import time
import unittest

from virtualkvm.app import HostController, ViewerController
from virtualkvm.control import ViewerClient, ProtocolError
from virtualkvm.media import StreamConfig
from virtualkvm.pairing import Permissions
from virtualkvm.security import SecurityError


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.02)
    assert predicate(), 'Condition did not become true before its deadline'


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.host = HostController(self.root / 'host', bind='127.0.0.1', port=0, synthetic=True)
        self.host.start()
        self.addCleanup(self.host.stop)
        self.client = ViewerClient(self.root / 'viewer')
        self.addCleanup(self.client.disconnect)

    def quick(self, **kwargs):
        return self.client.connect(*self.host.server.address, fingerprint=self.host.fingerprint,
                                   approval=True, name='Test computer', **kwargs)

    def test_tls_session_code_matches_both_sides_and_changes_between_connections(self):
        pairs = []
        def approve(request):
            pairs.append(('host', request.code))
            request.resolve(Permissions())
        self.host.server.on_access_request = approve
        for _ in range(2):
            self.quick(on_pairing_code=lambda code: pairs.append(('viewer', code)))
            self.assertEqual(pairs[-1][1], pairs[-2][1])
            self.assertEqual(len(pairs[-1][1].replace(' ', '')), 12)
            rival = ViewerClient(self.root / 'rival')
            confirmations = []
            try:
                with self.assertRaises(SecurityError):
                    rival.connect(*self.host.server.address, fingerprint=self.host.fingerprint,
                                  approval=True, name='Rival', confirm_pairing=lambda code: confirmations.append(code))
                self.assertFalse(confirmations, 'A busy host must not show a misleading code-confirmation dialog')
            finally:
                rival.disconnect()
            self.client.disconnect()
            wait_for(lambda: not self.host.server.sessions)
        self.assertNotEqual(pairs[0][1], pairs[2][1])
        self.assertNotIn(self.host.pin, repr(pairs))

    def test_view_only_host_permissions_block_input_clipboard_files_and_audio(self):
        self.host.server.on_access_request = lambda request: request.resolve(Permissions(control=False))
        self.quick()
        self.assertFalse(self.client.permissions.control)
        for kind, payload in [('event', {'event': {'type': 'key', 'key': 'a', 'down': True}}),
                              ('clipboard', {'text': 'private'}), ('file_begin', {'name': 'x', 'size': 0}),
                              ('shared_list', {}), ('stream_request', {'displays': [0], 'config': {'audio': True}})]:
            with self.subTest(kind=kind), self.assertRaises(ProtocolError):
                self.client._request(kind, **payload)
        self.assertFalse(self.host.native.events)
        self.client._request('event', event={'type': 'release_all'})

    def test_denied_request_never_starts_capture_or_leaves_an_active_session(self):
        self.host.server.on_access_request = lambda request: request.resolve()
        with self.assertRaises(SecurityError):
            self.quick()
        self.assertFalse(self.host.server.sessions)
        self.assertFalse(self.host.senders)
        self.assertFalse(self.host.native.events)

    def test_viewer_must_confirm_first_session_code_before_connecting(self):
        self.host.server.on_access_request = lambda request: request.resolve(Permissions())
        with self.assertRaises(SecurityError):
            self.quick(confirm_pairing=lambda code: False)
        wait_for(lambda: not self.host.server.sessions)
        self.assertFalse(self.client.connected)
        self.assertFalse(self.host.senders)

    def test_cancel_pending_approval_releases_both_connections_promptly(self):
        requests, errors = [], []
        self.host.server.on_access_request = requests.append
        def connect():
            try:self.quick()
            except Exception as error:errors.append(error)
        worker = threading.Thread(target=connect)
        worker.start()
        try:
            wait_for(lambda: bool(requests))
            self.client.cancel_connect()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            wait_for(lambda: requests[0].done.is_set(), timeout=2)
            self.assertFalse(self.host.server.sessions)
            self.assertTrue(errors)
        finally:
            requests[0].resolve() if requests else None
            worker.join(timeout=3)

    def test_media_tickets_are_single_use_and_invalid_keys_do_not_drop_control(self):
        self.client.connect(*self.host.server.address, pin=self.host.pin, fingerprint=self.host.fingerprint)
        config = dict(asdict(StreamConfig(width=160,height=90,fps=30)), transport='tcp', receiver_ports={'0':41001})
        stream = self.client.request_stream([0], config)['streams'][0]
        with self.assertRaises((EOFError, ProtocolError, OSError)):
            self.client.open_media(stream['ticket'], 'wrong-media-secret')
        media = self.client.open_media(stream['ticket'], stream['passphrase'])
        try:
            with self.assertRaises((EOFError, ProtocolError, OSError)):
                self.client.open_media(stream['ticket'], stream['passphrase'])
            self.assertTrue(self.client.connected)
            self.assertIn('time', self.client._request('ping'))
        finally:
            media.close()

    def test_real_two_screen_tcp_video_and_control_after_owner_approval(self):
        codes, frames = [], {}
        def approve(request):
            codes.append(request.code);request.resolve(Permissions(True,True,True,False))
        self.host.server.on_access_request = approve
        viewer = ViewerController(self.root / 'tcp-viewer', on_frame=lambda i,*args: frames.update({i:args}))
        try:
            viewer.connect(*self.host.server.address, pin=None, fingerprint=self.host.fingerprint, displays=[0,1],
                           config=dict(asdict(StreamConfig(width=160,height=90,fps=30,encoder='libx264')),transport='tcp'),
                           approval=True, name='Viewer', on_pairing_code=codes.append)
            wait_for(lambda: set(frames)=={0,1})
            self.assertEqual(codes[0],codes[1])
            viewer.send_event({'type':'key','key':'a','down':True})
            wait_for(lambda: any(event.get('key')=='a' and event.get('down') for event in self.host.native.events))
            viewer.disconnect()
            wait_for(lambda: not self.host.senders)
            self.assertTrue(any(event.get('key')=='a' and event.get('down') is False for event in self.host.native.events))
        finally:
            viewer.disconnect()
