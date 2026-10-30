"""Shared-folder desktop actions against an authenticated TLS connection."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from common_qt import wait_ms
from PySide6.QtWidgets import QApplication

from virtualkvm.control import HostServer, ViewerClient
from virtualkvm.shared_gui import SharedFolderDialog


class SharedFolderGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="virtualkvm-shared-gui-")
        self.root = Path(self.temporary.name)
        self.shared = self.root / "shared"
        self.shared.mkdir()
        (self.shared / "documents").mkdir()
        (self.shared / "documents" / "nested.txt").write_text("Nested content", encoding="utf-8")
        (self.shared / "hello.txt").write_bytes(b"Shared network content\x00\xff")
        self.local = self.root / "local"
        self.local.mkdir()
        self.server = self.viewer = None
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            if not dialog._closed:
                dialog.close()
        self.app.processEvents()
        if self.viewer:
            self.viewer.disconnect()
        if self.server:
            self.server.stop()
        self.temporary.cleanup()

    def connect(self, *, writable=True, enabled=True):
        pin = "shared-gui-test-pairing-123"
        self.server = HostServer("127.0.0.1", 0,
            state_dir=self.root / "host-state", pin=pin, on_event=lambda _: None,
            shared_dir=self.shared if enabled else None, shared_writable=writable)
        self.viewer = ViewerClient(self.root / "viewer-state")
        self.server.start()
        self.viewer.connect("127.0.0.1", self.server.address[1], pin, self.server.fingerprint)
        return self.viewer

    def open_dialog(self, viewer=None):
        dialog = SharedFolderDialog(viewer or self.viewer)
        self.dialogs.append(dialog)
        dialog.show()
        self.wait_ready(dialog)
        return dialog

    def wait_ready(self, dialog):
        deadline = time.monotonic() + 6
        while dialog._busy and time.monotonic() < deadline:
            wait_ms(20)
        self.assertFalse(dialog._busy, dialog.status_label.text())

    def select(self, dialog, name):
        for index in range(dialog.tree.topLevelItemCount()):
            item = dialog.tree.topLevelItem(index)
            if item.text(0) == name:
                dialog.tree.setCurrentItem(item)
                return item
        self.fail(f"Shared folder row missing: {name}; {dialog.status_label.text()}")

    def test_authenticated_navigation_download_upload_and_new_directory(self):
        self.connect()
        dialog = self.open_dialog()
        self.assertEqual(dialog.tree.topLevelItemCount(), 2)
        self.assertEqual(dialog.tree.topLevelItem(0).text(0), "documents")
        self.assertEqual(dialog.permission_label.text(), "Чтение и запись")
        self.assertTrue(dialog.upload_button.isEnabled())
        directory = self.select(dialog, "documents")
        dialog._open_item(directory)
        self.wait_ready(dialog)
        self.assertEqual(dialog.current_path, "documents")
        self.assertEqual(dialog.tree.topLevelItemCount(), 1)
        self.select(dialog, "nested.txt")
        destination = self.local / "nested.txt"
        with mock.patch("virtualkvm.shared_gui.QFileDialog.getSaveFileName",
                        return_value=(str(destination), "")):
            dialog._download()
            self.wait_ready(dialog)
        self.assertEqual(destination.read_text(encoding="utf-8"), "Nested content")
        self.assertFalse(any(self.local.glob("*.part")))
        dialog._go_up()
        self.wait_ready(dialog)
        self.assertEqual(dialog.current_path, "")
        upload = self.local / "uploaded.bin"
        payload = bytes(range(256)) * 1025
        upload.write_bytes(payload)
        with mock.patch("virtualkvm.shared_gui.QFileDialog.getOpenFileName",
                        return_value=(str(upload), "")):
            dialog._upload()
            self.wait_ready(dialog)
        self.assertEqual((self.shared / upload.name).read_bytes(), payload)
        self.select(dialog, upload.name)
        with mock.patch("virtualkvm.shared_gui.QInputDialog.getText",
                        return_value=("created-from-desktop", True)):
            dialog._make_directory()
            self.wait_ready(dialog)
        self.assertTrue((self.shared / "created-from-desktop").is_dir())
        self.select(dialog, "created-from-desktop")

    def test_readonly_allows_download_but_disables_mutating_actions(self):
        self.connect(writable=False)
        dialog = self.open_dialog()
        self.assertEqual(dialog.permission_label.text(), "Только чтение")
        self.assertFalse(dialog.upload_button.isEnabled())
        self.assertFalse(dialog.mkdir_button.isEnabled())
        with mock.patch("virtualkvm.shared_gui.QFileDialog.getOpenFileName") as upload_chooser, \
             mock.patch("virtualkvm.shared_gui.QInputDialog.getText") as directory_name:
            dialog._upload()
            dialog._make_directory()
            upload_chooser.assert_not_called()
            directory_name.assert_not_called()
        self.select(dialog, "hello.txt")
        self.assertTrue(dialog.download_button.isEnabled())
        destination = self.local / "readonly-download.bin"
        with mock.patch("virtualkvm.shared_gui.QFileDialog.getSaveFileName",
                        return_value=(str(destination), "")):
            dialog._download()
            self.wait_ready(dialog)
        self.assertEqual(destination.read_bytes(), (self.shared / "hello.txt").read_bytes())

    def test_upload_collision_preserves_existing_file_and_reports_error(self):
        self.connect()
        dialog = self.open_dialog()
        original = (self.shared / "hello.txt").read_bytes()
        replacement = self.local / "hello.txt"
        replacement.write_bytes(b"Must not replace the host file")
        with mock.patch("virtualkvm.shared_gui.QFileDialog.getOpenFileName",
                        return_value=(str(replacement), "")):
            dialog._upload()
            self.wait_ready(dialog)
        self.assertEqual((self.shared / "hello.txt").read_bytes(), original)
        self.assertIn("Не удалось выполнить действие", dialog.status_label.text())
        self.assertTrue(dialog.upload_button.isEnabled())

    def test_disabled_share_explains_opt_in_without_listing_host(self):
        viewer = self.connect(enabled=False)
        self.assertFalse(viewer.shared_info["enabled"])
        with mock.patch.object(viewer, "shared_list", wraps=viewer.shared_list) as listing:
            dialog = self.open_dialog()
            listing.assert_not_called()
        self.assertEqual(dialog.stack.currentIndex(), 1)
        self.assertEqual(dialog.empty_title.text(), "Общая папка не подключена")
        self.assertFalse(dialog.upload_button.isEnabled())
        self.assertFalse(dialog.mkdir_button.isEnabled())
        self.assertFalse(dialog.download_button.isEnabled())

    def test_closing_during_pending_listing_ignores_late_result(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        threads = []

        class SlowViewer:
            shared_info = {"enabled": True, "writable": False}

            def shared_list(self, path):
                threads.append(threading.current_thread())
                entered.set()
                release.wait(timeout=5)
                completed.set()
                return {"path": "late-result", "entries": [], "writable": False}

        failures = []
        old_hook = threading.excepthook
        threading.excepthook = failures.append
        try:
            dialog = SharedFolderDialog(SlowViewer())
            self.dialogs.append(dialog)
            self.assertTrue(entered.wait(timeout=2))
            self.assertTrue(dialog._busy)
            self.assertNotEqual(threads[0], threading.current_thread())
            dialog.close()
            self.app.processEvents()
            release.set()
            self.assertTrue(completed.wait(timeout=2))
            threads[0].join(timeout=2)
            self.app.processEvents()
            self.assertTrue(dialog._closed)
            self.assertEqual(dialog.current_path, "")
            self.assertEqual(failures, [])
        finally:
            release.set()
            threading.excepthook = old_hook


if __name__ == "__main__":
    unittest.main()
