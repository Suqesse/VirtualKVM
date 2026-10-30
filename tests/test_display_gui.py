"""Real Qt layout editing and encrypted discovery/three-stream presentation."""
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QPoint, QRect, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLabel

from common_qt import wait_ms
from virtualkvm.display_gui import LayoutDialog, local_screens
from virtualkvm.display_layout import Placement, arrange
from virtualkvm.gui import MainWindow
from virtualkvm.app import HostController


class DisplayGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)
        self.window = MainWindow(self.state / "viewer")
        self.window.show()
        wait_ms(20)

    def wait_for(self, condition, timeout=20):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            wait_ms(20)
        self.assertTrue(condition(), self.window.log.toPlainText())

    def tearDown(self):
        self.window.close()
        self.wait_for(lambda: self.window._shutdown_complete)
        self.temporary.cleanup()

    def test_dialog_drag_and_keyboard_move_the_configured_window(self):
        panel = self.window.monitor_layout
        original = panel.encode()
        dialog = LayoutDialog([Placement(0), Placement(2, x=2000)], [], "windows", False, self.window)
        try:
            dialog.show()
            wait_ms(30)
            center = dialog.map._geometry()[1].center().toPoint()
            before = dialog.entries[1]
            QTest.mousePress(dialog.map, Qt.MouseButton.LeftButton, pos=center)
            QTest.mouseMove(dialog.map, center + QPoint(20, 25))
            QTest.mouseRelease(dialog.map, Qt.MouseButton.LeftButton, pos=center + QPoint(20, 25))
            self.assertNotEqual((dialog.entries[1].x, dialog.entries[1].y), (before.x, before.y))
            self.assertEqual(dialog.table.cellWidget(1, 2).value(), dialog.entries[1].x)
            x = dialog.entries[1].x
            QTest.keyClick(dialog.map, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
            self.assertEqual(dialog.entries[1].x, x + 10)
            dialog.reject()
            self.assertEqual(panel.encode(), original, "Cancel must leave the applied layout untouched")
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_apply_persists_eight_screen_layout_without_pairing_secrets(self):
        self.window.peer_pin.setText("private-layout-pin")
        self.window.peer_fingerprint.setText("f" * 64)
        def edit():
            dialog = QApplication.activeModalWidget()
            dialog.count.setValue(8)
            dialog.table.cellWidget(7, 2).setValue(-800)
            dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).click()
        QTimer.singleShot(30, edit)
        self.window.monitor_layout._configure()
        self.assertEqual(len(self.window.monitor_layout.entries), 8)
        self.assertEqual(self.window.monitor_layout.entries[7].x, -800)
        data = json.loads((self.window.state_dir / "preferences.json").read_text())
        self.assertEqual(len(data["monitor_layout"]["entries"]), 8)
        self.assertNotIn("private-layout-pin", json.dumps(data))
        self.assertNotIn("f" * 64, json.dumps(data))
        self.window.close()
        self.wait_for(lambda: self.window._shutdown_complete)
        self.window = MainWindow(self.state / "viewer")
        self.assertEqual(len(self.window.monitor_layout.entries), 8)
        self.assertEqual(self.window.monitor_layout.entries[7].x, -800)

    def test_legacy_two_monitor_preferences_migrate(self):
        self.window.close()
        self.wait_for(lambda: self.window._shutdown_complete)
        (self.window.state_dir / "preferences.json").write_text(json.dumps({
            "first_display": 2, "second_display": 5, "second_display_enabled": True}))
        self.window = MainWindow(self.state / "viewer")
        self.assertEqual([e.source for e in self.window.monitor_layout.entries], [2, 5])
        self.assertFalse(self.window.peer_pin.text())

    def test_mapping_uses_local_coordinates_and_fullscreen_target(self):
        screen = SimpleNamespace(geometry=lambda: QRect(-1600, -100, 1600, 900),
                                 availableGeometry=lambda: QRect(-1600, -100, 1600, 860))
        self.window.monitor_layout.set_entries([Placement(7, target="left")], "monitors", True)
        with patch("virtualkvm.gui.local_screens", return_value=[("left", "Left", screen)]):
            rectangle, target, fullscreen = self.window._window_placement(7)
        self.assertEqual(rectangle, (-1600, -100, 1600, 900))
        self.assertIs(target, screen)
        self.assertTrue(fullscreen)
        key, _, actual = local_screens()[0]
        self.window.monitor_layout.set_entries([Placement(7, target=key)], "monitors", True)
        video = self.window._video_window(7)
        wait_ms(20)
        self.assertTrue(video.isFullScreen())
        self.assertIs(video.windowHandle().screen(), actual)

    def test_compact_dialog_keeps_apply_available_and_duplicate_sources_rejected(self):
        dialog = LayoutDialog([Placement(0), Placement(1, x=2000)], [], "windows", False, self.window)
        try:
            dialog.resize(640, 440)
            dialog.show()
            wait_ms(30)
            apply = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
            self.assertLess(apply.mapTo(dialog, QPoint(0, 0)).y() + apply.height(), dialog.height())
            self.assertGreater(dialog.scroll.verticalScrollBar().maximum(), 0)
            dialog.table.cellWidget(1, 0).setCurrentIndex(0)
            self.assertFalse(apply.isEnabled())
            self.assertIn("один раз", dialog.error.text())
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_stale_discovery_does_not_replace_another_hosts_layout(self):
        self.window.peer_host.setText("old.example")
        self.window.peer_pin.setText("old-layout-pin")
        self.window.peer_fingerprint.setText("a" * 64)
        old = self.window._peer_signature()
        self.window._scanning_displays = True
        self.window.peer_host.setText("new.example")
        self.window._finished("display-discovery", 0, {"peer": old, "displays": [dict(
            index=7, name="Old", x=0, y=0, width=1920, height=1080)]}, None)
        self.assertFalse(self.window.monitor_layout.inventory)
        self.assertTrue(self.window.connect_button.isEnabled())

    def test_eight_pending_frames_are_kept_and_unselected_sources_are_ignored(self):
        self.window._requested_displays = list(range(8))
        for index in range(8):
            self.window._receive_frame(self.window._viewer_epoch, index, b"\x00\x00\x00", 1, 1)
        self.window._receive_frame(self.window._viewer_epoch, 9, b"\x00\x00\x00", 1, 1)
        self.window._receive_frame(self.window._viewer_epoch, True, b"\x00\x00\x00", 1, 1)
        self.assertEqual(set(self.window._frames), set(range(8)))

    def test_new_viewer_connects_to_a_legacy_host_without_new_stream_options(self):
        from virtualkvm.control import _Session
        original_send = _Session.send
        def legacy_send(session, message):
            if message.get("type") == "authenticated":
                message = dict(message)
                message.pop("capabilities", None)
            return original_send(session, message)
        host = HostController(self.state / "legacy-host", bind="127.0.0.1", port=0,
                              synthetic=True, pin="legacy-host-test-pin")
        prepare = host._prepare_streams
        seen = []
        def legacy_prepare(request):
            self.assertNotIn("preserve_aspect", request["config"])
            seen.append(request)
            return prepare(request)
        host.server.on_stream_request = legacy_prepare
        with patch.object(_Session, "send", legacy_send):
            try:
                host.start()
                window = self.window
                window.peer_host.setText("127.0.0.1")
                window.peer_port.setValue(host.server.address[1])
                window.peer_pin.setText(host.pin)
                window.peer_fingerprint.setText(host.fingerprint)
                window.video_width.setValue(160)
                window.video_height.setValue(90)
                window.frame_rate.setValue(30)
                window.encoder.setCurrentText("libx264")
                window.connect_button.click()
                self.wait_for(lambda: bool(window.video_windows), timeout=25)
                self.assertTrue(seen)
                self.assertEqual(window._viewer.client.capabilities["max_streams"], 2)
                window.disconnect_button.click()
                self.wait_for(lambda: window._viewer is None)
            finally:
                if self.window._viewer is not None:
                    self.window._disconnect_viewer()
                    self.wait_for(lambda: self.window._viewer is None)
                host.stop()

    def test_authenticated_discovery_and_three_portrait_landscape_streams(self):
        host = HostController(self.state / "host", bind="127.0.0.1", port=0, synthetic=True,
                              pin="three-screen-test-pin")
        displays = [dict(index=0, name="Main", x=0, y=0, width=1920, height=1080),
                    dict(index=2, name="Portrait", x=-1080, y=0, width=1080, height=1920),
                    dict(index=5, name="Upper", x=0, y=-900, width=1600, height=900)]
        host.displays[:] = displays
        try:
            host.start()
            window = self.window
            window.peer_host.setText("127.0.0.1")
            window.peer_port.setValue(host.server.address[1])
            window.peer_pin.setText(host.pin)
            window.peer_fingerprint.setText(host.fingerprint)
            window.monitor_layout.set_entries([Placement(0), Placement(2), Placement(5)])
            window.monitor_layout.discover.click()
            self.wait_for(lambda: not window._scanning_displays)
            self.assertEqual([d.index for d in window.monitor_layout.inventory], [0, 2, 5])
            self.assertFalse(host.senders, "Inventory discovery must not start screen capture")
            self.wait_for(lambda: not host.server.sessions)
            window.monitor_layout.set_entries(arrange(window.monitor_layout.entries, "vertical", window.monitor_layout.inventory))
            # Connect with only manually configured positions, as on first use
            # without a cached inventory; negotiated portrait geometry must win.
            window.monitor_layout.inventory = []
            window.monitor_layout.set_entries([Placement(e.source, e.x, e.y)
                                               for e in window.monitor_layout.entries])
            window.video_width.setValue(160)
            window.video_height.setValue(90)
            window.frame_rate.setValue(30)
            window.encoder.setCurrentText("libx264")
            window.connect_button.click()
            self.wait_for(lambda: len(window.video_windows) == 3, timeout=30)
            self.wait_for(lambda: all(not v.canvas._image.isNull() for v in window.video_windows.values()))
            self.assertEqual(set(window.video_windows), {0, 2, 5})
            self.assertEqual(len(window._viewer.receivers), 3)
            self.assertEqual(host.displays, displays, "Receiving-window layout must not alter source desktop geometry")
            self.assertLess(window.video_windows[2].canvas._image.width(), window.video_windows[2].canvas._image.height())
            self.assertLess(window.video_windows[2].width(), window.video_windows[2].height())
            window.control_enabled.setChecked(True)
            canvas = window.video_windows[5].canvas
            from PySide6.QtCore import QEvent
            from PySide6.QtGui import QMouseEvent
            center = canvas.image_rect().center()
            for kind, buttons in ((QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
                                  (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton)):
                event = QMouseEvent(kind, center, center, Qt.MouseButton.LeftButton, buttons, Qt.KeyboardModifier.NoModifier)
                (canvas.mousePressEvent if kind == QEvent.Type.MouseButtonPress else canvas.mouseReleaseEvent)(event)
            self.wait_for(lambda: any(e.get("type") == "pointer" for e in host.native.events))
            pointers = [e for e in host.native.events if e.get("type") == "pointer"]
            self.assertEqual(pointers[-1]["display"], 5)
            self.assertAlmostEqual(pointers[-1]["x"], 0.5, delta=0.02)
            self.assertAlmostEqual(pointers[-1]["y"], 0.5, delta=0.02)
            window.disconnect_button.click()
            self.wait_for(lambda: window._viewer is None)
            self.assertFalse(window.video_windows)
            self.wait_for(lambda: not host.senders)
        finally:
            if self.window._viewer is not None:
                self.window._disconnect_viewer()
                self.wait_for(lambda: self.window._viewer is None)
            host.stop()


if __name__ == "__main__":
    unittest.main()
