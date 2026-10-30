"""Confined shared-folder behavior through the real authenticated TLS API."""
import base64
import hashlib
import os
from pathlib import Path
import tempfile
import time
import unittest

from virtualkvm.control import HostServer, ViewerClient, ProtocolError
from virtualkvm.shared import SharedDirectory, SharedError, _open_windows_shared_file


class SharedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.share = self.root / "Shared"
        self.share.mkdir()
        (self.share / "subfolder").mkdir()
        (self.share / "hello.txt").write_text("Hello — привет", encoding="utf-8")
        self.host = None
        self.client = None

    def tearDown(self):
        if self.client:
            self.client.disconnect()
        if self.host:
            self.host.stop()
        self.temporary.cleanup()

    def connect(self, writable=False, enabled=True):
        self.host = HostServer("127.0.0.1", 0, state_dir=self.root / "identity", pin="123456789012",
            on_event=lambda _: None, receive_dir=self.root / "received",
            shared_dir=self.share if enabled else None, shared_writable=writable)
        self.host.start()
        self.client = ViewerClient(self.root / "viewer")
        self.client.connect(*self.host.address, pin="123456789012", fingerprint=self.host.fingerprint)
        return self.client

    def test_authenticated_browse_readonly_and_download(self):
        client = self.connect()
        self.assertEqual(client.shared_info, {"enabled": True, "writable": False, "name": "Shared"})
        listing = client.shared_list()
        self.assertEqual(listing["path"], "")
        self.assertEqual([item["name"] for item in listing["entries"]], ["subfolder", "hello.txt"])
        self.assertEqual(listing["entries"][0]["type"], "directory")
        self.assertIsInstance(listing["entries"][1]["mtime"], float)
        destination = self.root / "download.txt"
        result = client.shared_download("hello.txt", destination)
        self.assertEqual(destination.read_bytes(), (self.share / "hello.txt").read_bytes())
        self.assertEqual(result["sha256"], hashlib.sha256(destination.read_bytes()).hexdigest())
        with self.assertRaises(ProtocolError):
            client.shared_download("hello.txt", destination)

    def test_readonly_denies_upload_and_mkdir(self):
        client = self.connect()
        with self.assertRaisesRegex(ProtocolError, "read-only"):
            client.shared_upload(self.share / "hello.txt", "new.txt")
        with self.assertRaisesRegex(ProtocolError, "read-only"):
            client.shared_mkdir("newfolder")
        self.assertFalse((self.share / "new.txt").exists())
        self.assertFalse((self.share / "newfolder").exists())

    def test_writable_upload_download_multichunk_and_empty(self):
        client = self.connect(writable=True)
        self.assertTrue(client.shared_info["writable"])
        self.assertEqual(client.shared_mkdir("subfolder/created"), {"path": "subfolder/created"})
        source = self.root / "source.bin"
        payload = bytes(range(256)) * 1025
        source.write_bytes(payload)
        result = client.shared_upload(source, "subfolder/created/upload.bin")
        self.assertEqual(result["path"], "subfolder/created/upload.bin")
        self.assertEqual((self.share / result["path"]).read_bytes(), payload)
        destination = self.root / "returned.bin"
        client.shared_download(result["path"], destination)
        self.assertEqual(destination.read_bytes(), payload)
        with self.assertRaises((ProtocolError, OSError)):
            client.shared_upload(source, result["path"])
        source.write_bytes(b"")
        client.shared_upload(source, "empty")
        client.shared_download("empty", self.root / "empty-returned")
        self.assertEqual((self.root / "empty-returned").stat().st_size, 0)
        self.assertFalse(list(self.share.rglob("*.part")))

    def test_disabled_by_default(self):
        client = self.connect(enabled=False)
        self.assertFalse(client.shared_info["enabled"])
        with self.assertRaisesRegex(ProtocolError, "not enabled"):
            client.shared_list()

    def test_paths_cannot_escape_root(self):
        client = self.connect(writable=True)
        source = self.root / "source"
        source.write_bytes(b"x")
        for remote in ("../secret", "/etc", "subfolder/../../secret", "C:/Windows", "..\\secret", "subfolder//x", "subfolder/./x", "CON", ".virtualkvm-attack"):
            with self.subTest(remote=remote):
                with self.assertRaises(ProtocolError):
                    client.shared_list(remote)
                with self.assertRaises(ProtocolError):
                    client.shared_upload(source, remote)
        self.assertFalse((self.root / "secret").exists())

    @unittest.skipIf(os.name == "nt", "Test symlink creation requires Unix")
    def test_symlinks_hardlinks_and_special_files_are_not_exposed(self):
        outside = self.root / "private"
        outside.mkdir()
        secret = outside / "secret"
        secret.write_bytes(b"private")
        (self.share / "escape").symlink_to(outside, target_is_directory=True)
        (self.share / "alias").symlink_to(secret)
        os.link(secret, self.share / "hardlink")
        os.mkfifo(self.share / "pipe")
        client = self.connect(writable=True)
        names = [item["name"] for item in client.shared_list()["entries"]]
        for unsafe in ("escape", "alias", "hardlink", "pipe"):
            self.assertNotIn(unsafe, names)
        for path in ("escape/secret", "alias", "hardlink", "pipe"):
            with self.assertRaises(ProtocolError):
                client.shared_download(path, self.root / "leaked")
        with self.assertRaises(ProtocolError):
            client.shared_upload(self.share / "hello.txt", "escape/new")
        self.assertFalse((outside / "new").exists())

    def test_private_identity_folder_cannot_be_shared_directly_or_indirectly(self):
        identity = self.root / "identity"
        identity.mkdir()
        for root in (identity, self.root):
            with self.assertRaises(SharedError):
                SharedDirectory(root, forbidden=identity)

    def test_upload_interrupt_cleans_partial_files_and_shared_transfer_excludes_legacy(self):
        client = self.connect(writable=True)
        client._request("shared_write_begin", path="aborted", size=30)
        client._request("shared_write_chunk", data=base64.b64encode(b"some bytes").decode())
        client.disconnect()
        deadline = time.monotonic() + 2
        while self.host.sessions and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse((self.share / "aborted").exists())
        self.assertFalse(list(self.share.glob("*.part")))
        client.connect(*self.host.address, pin="123456789012", fingerprint=self.host.fingerprint)
        client._request("file_begin", name="legacy", size=30)
        with self.assertRaisesRegex(ProtocolError, "already in progress"):
            client._request("shared_read_begin", path="hello.txt")
        payload = b"x" * 30
        client._request("file_chunk", data=base64.b64encode(payload).decode())
        client._request("file_end", sha256=hashlib.sha256(payload).hexdigest())
        self.assertEqual((self.root / "received" / "legacy").read_bytes(), payload)
        self.assertFalse(list((self.root / "received").glob("*.part")))

    def test_changed_source_rejected_and_upload_checksum_failure_cleanup(self):
        client = self.connect(writable=True)
        source = self.share / "changing"
        source.write_bytes(b"a" * (128 * 1024))
        client._request("shared_read_begin", path="changing")
        client._request("shared_read", offset=0, length=64 * 1024)
        if os.name == "nt":
            # Windows enforces an immutable source handle: attempted writes
            # fail before truncation, and the reader finishes the original file.
            with self.assertRaises(PermissionError):
                source.write_bytes(b"b" * (128 * 1024))
            final = client._request("shared_read", offset=64 * 1024, length=64 * 1024)
            self.assertEqual(base64.b64decode(final["data"]), b"a" * (64 * 1024))
            self.assertTrue(final["eof"])
            self.assertEqual(final["sha256"], hashlib.sha256(b"a" * (128 * 1024)).hexdigest())
            source.write_bytes(b"b" * (128 * 1024))  # EOF releases the lock.
        else:
            source.write_bytes(b"b" * (128 * 1024))
            with self.assertRaisesRegex(ProtocolError, "changed"):
                client._request("shared_read", offset=64 * 1024, length=64 * 1024)
        client._request("shared_write_begin", path="bad", size=1)
        client._request("shared_write_chunk", data=base64.b64encode(b"x").decode())
        with self.assertRaisesRegex(ProtocolError, "SHA-256"):
            client._request("shared_write_end", sha256="00" * 32)
        self.assertFalse((self.share / "bad").exists())
        self.assertFalse(list(self.share.glob("*.part")))

    def test_source_mutation_rejected_even_when_windows_handle_metadata_is_cached(self):
        from unittest.mock import patch
        client = self.connect()
        source = self.share / "cached-stat"
        original = b"a" * (128 * 1024)
        # Exercise unread data and an already-delivered region. In the second
        # case the remaining bytes would still match the original. Windows
        # must deny the write; POSIX must detect it despite cached fstat.
        for replacement in (b"b" * len(original), b"b" * (64 * 1024) + original[64 * 1024:]):
            with self.subTest(already_sent_only=replacement[64 * 1024:] == original[64 * 1024:]):
                source.write_bytes(original)
                cached = source.stat()
                with patch("virtualkvm.shared.os.fstat", return_value=cached):
                    client._request("shared_read_begin", path="cached-stat")
                    first = client._request("shared_read", offset=0, length=64 * 1024)
                    self.assertEqual(base64.b64decode(first["data"]), original[:64 * 1024])
                    if os.name == "nt":
                        with self.assertRaises(PermissionError):
                            source.write_bytes(replacement)
                        final = client._request("shared_read", offset=64 * 1024, length=64 * 1024)
                        self.assertEqual(base64.b64decode(final["data"]), original[64 * 1024:])
                        self.assertTrue(final["eof"])
                        self.assertEqual(final["sha256"], hashlib.sha256(original).hexdigest())
                    else:
                        source.write_bytes(replacement)
                        with self.assertRaisesRegex(ProtocolError, "changed"):
                            client._request("shared_read", offset=64 * 1024, length=64 * 1024)
        # Both failures must close their transfer and leave the session usable.
        client.shared_download("hello.txt", self.root / "after-mutation.txt")
        self.assertEqual((self.root / "after-mutation.txt").read_bytes(), (self.share / "hello.txt").read_bytes())

    def test_windows_shared_handle_denies_write_delete_and_transfers_ownership(self):
        import ctypes
        import types
        from unittest.mock import Mock, patch
        kernel = Mock()
        kernel.CreateFileW.return_value = 1234
        def attribute_info(handle, kind, target, size):
            ctypes.cast(target, ctypes.POINTER(ctypes.c_uint32))[0] = 0x80
            return 1
        kernel.GetFileInformationByHandleEx.side_effect = attribute_info
        conversion = Mock(return_value=123)
        with patch("ctypes.WinDLL", return_value=kernel, create=True), \
                patch.dict("sys.modules", {"msvcrt": types.SimpleNamespace(open_osfhandle=conversion)}):
            self.assertEqual(_open_windows_shared_file(self.share / "hello.txt"), 123)
            arguments = kernel.CreateFileW.call_args.args
            self.assertEqual(arguments[1], 0x80000000)  # GENERIC_READ
            self.assertEqual(arguments[2], 0x1)  # READ sharing, no WRITE/DELETE
            self.assertEqual(arguments[4], 3)  # OPEN_EXISTING
            self.assertTrue(arguments[5] & 0x00200000)  # OPEN_REPARSE_POINT
            conversion.assert_called_once_with(1234, os.O_RDONLY | getattr(os, "O_BINARY", 0))
            kernel.CloseHandle.assert_not_called()  # CRT descriptor owns it.
            conversion.side_effect = OSError("descriptor allocation failed")
            with self.assertRaises(OSError):
                _open_windows_shared_file(self.share / "hello.txt")
            kernel.CloseHandle.assert_called_once_with(1234)

    @unittest.skipUnless(os.name == "nt", "Verifies native Windows file-sharing enforcement")
    def test_windows_download_lock_blocks_replacement_and_is_released_on_disconnect(self):
        client = self.connect()
        source = self.share / "locked.bin"
        source.write_bytes(b"a" * (128 * 1024))
        client._request("shared_read_begin", path="locked.bin")
        client._request("shared_read", offset=0, length=64 * 1024)
        with self.assertRaises(PermissionError):
            source.unlink()
        with self.assertRaises(PermissionError):
            source.rename(self.share / "renamed.bin")
        client.disconnect()
        deadline = time.monotonic() + 2
        while self.host.sessions and time.monotonic() < deadline:
            time.sleep(0.01)
        source.rename(self.share / "renamed.bin")
        (self.share / "renamed.bin").unlink()

    def test_directory_listing_is_bounded(self):
        from unittest.mock import patch
        client = self.connect()
        with patch("virtualkvm.shared.MAX_ENTRIES", 1):
            with self.assertRaisesRegex(ProtocolError, "browsing limit"):
                client.shared_list()

    def test_legacy_file_drop_cannot_bypass_a_readonly_share(self):
        client = self.connect()
        self.host.receive_dir = self.share
        source = self.root / "injected"
        source.write_bytes(b"x")
        with self.assertRaisesRegex(ProtocolError, "read-only"):
            client.send_file(source)
        self.assertFalse((self.share / "injected").exists())

    @unittest.skipIf(os.name == "nt", "Test symlink creation requires Unix")
    def test_replaced_shared_root_is_refused(self):
        client = self.connect()
        outside = self.root / "outside"
        outside.mkdir()
        self.share.rename(self.root / "moved")
        self.share.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ProtocolError, "root changed"):
            client.shared_list()


if __name__ == "__main__":
    unittest.main()
