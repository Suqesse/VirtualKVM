"""Reserved/occupied listener ports must fall back without requiring elevation."""
import errno
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from virtualkvm.control import HostServer


class ListenerPortTests(unittest.TestCase):
    def test_reserved_windows_port_is_replaced_by_an_os_allocated_port(self):
        with tempfile.TemporaryDirectory() as directory:
            statuses = []
            host = HostServer(state_dir=Path(directory), pin='valid-host-pin', on_event=lambda _: None,
                              allow_port_fallback=True, on_status=statuses.append)
            denied = OSError(errno.EACCES, 'reserved port')
            denied.winerror = 10013
            listener = Mock()
            listener.bind.side_effect = [denied, None]
            listener.getsockname.return_value = ('0.0.0.0', 50321)
            # No fake accept thread; this test isolates Winsock bind negotiation.
            with patch('virtualkvm.control.socket.socket', return_value=listener), patch('virtualkvm.control.threading.Thread'):
                host.start()
                self.assertEqual(listener.bind.call_args_list[0].args[0], ('0.0.0.0', 24800))
                self.assertEqual(listener.bind.call_args_list[1].args[0], ('0.0.0.0', 0))
                self.assertEqual(host.address[1], 50321)
                self.assertTrue(any('50321' in text for text in statuses))
                host.stop()

    def test_occupied_port_fallback_is_reachable_by_real_tcp(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            occupied.listen(1)
            host = HostServer(bind='127.0.0.1', port=occupied.getsockname()[1], state_dir=Path(directory),
                              pin='valid-host-pin', on_event=lambda _: None, allow_port_fallback=True)
            try:
                host.start()
                self.assertNotEqual(host.address[1], occupied.getsockname()[1])
                with socket.create_connection(host.address, timeout=2):
                    pass
            finally:
                host.stop()

    def test_fixed_port_and_non_port_errors_are_not_silently_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            for auto, code in [(False, errno.EADDRINUSE), (True, errno.EADDRNOTAVAIL)]:
                host = HostServer(state_dir=Path(directory), pin='valid-host-pin', on_event=lambda _: None,
                                  allow_port_fallback=auto)
                listener = Mock()
                listener.bind.side_effect = OSError(code, 'unavailable')
                with patch('virtualkvm.control.socket.socket', return_value=listener), self.assertRaises(OSError):
                    host.start()
                self.assertEqual(listener.bind.call_count, 1)
                listener.close.assert_called_once()
