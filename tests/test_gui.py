"""Offscreen UI/input checks plus real encrypted dual-stream presentation."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.environ.get("QT_QPA_PLATFORM") in ("offscreen", "minimal"):
    os.environ.setdefault("QT_AUDIO_BACKEND", "pulseaudio")

from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QFocusEvent, QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from virtualkvm.gui import MainWindow, VideoCanvas, VideoWindow, normalized_key
from common_qt import wait_ms


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.events = []
        self.emergencies = []
        self.canvas = VideoCanvas(1, self.events.append, lambda: self.emergencies.append(True))
        self.canvas.resize(640, 480)
        self.canvas.set_frame(bytes(320 * 180 * 3), 320, 180)
        self.canvas.set_input_enabled(True)

    def tearDown(self):
        self.canvas.release_pressed()
        self.canvas.deleteLater()
        self.app.processEvents()

    @staticmethod
    def key(kind, key, modifiers=Qt.KeyboardModifier.NoModifier, text=""):
        return QKeyEvent(kind, key, modifiers, text)

    @staticmethod
    def mouse(kind, x, y, button, buttons):
        position = QPointF(x, y)
        return QMouseEvent(kind, position, position, button, buttons, Qt.KeyboardModifier.NoModifier)

    def test_letterbox_coordinates_and_black_bars(self):
        rect = self.canvas.image_rect()
        self.assertEqual((rect.x(), rect.y(), rect.width(), rect.height()), (0, 60, 640, 360))
        self.canvas.mousePressEvent(self.mouse(QEvent.Type.MouseButtonPress, 320, 20,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
        self.assertEqual(self.events, [])
        self.canvas.mousePressEvent(self.mouse(QEvent.Type.MouseButtonPress, 320, 240,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
        self.assertEqual(self.events[0], {"type": "pointer", "display": 1, "x": 0.5, "y": 0.5})
        self.assertEqual(self.events[1], {"type": "button", "button": "left", "down": True})
        self.canvas.mouseReleaseEvent(self.mouse(QEvent.Type.MouseButtonRelease, 320, 20,
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton))
        self.assertEqual(self.events[-2]["y"], 0)
        self.assertEqual(self.events[-1], {"type": "button", "button": "left", "down": False})

    def test_double_click_forwards_both_down_up_pairs(self):
        press = self.mouse(QEvent.Type.MouseButtonPress, 200, 200,
                           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
        release = self.mouse(QEvent.Type.MouseButtonRelease, 200, 200,
                             Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)
        self.canvas.mousePressEvent(press)
        self.canvas.mouseReleaseEvent(release)
        self.canvas.mouseDoubleClickEvent(self.mouse(QEvent.Type.MouseButtonDblClick, 200, 200,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
        self.canvas.mouseReleaseEvent(release)
        self.assertEqual([e["down"] for e in self.events if e["type"] == "button"],
                         [True, False, True, False])

    def test_key_release_focus_loss_and_input_opt_in(self):
        self.canvas.set_input_enabled(False)
        self.canvas.keyPressEvent(self.key(QEvent.Type.KeyPress, Qt.Key.Key_A, text="a"))
        self.assertEqual(self.events, [])
        self.canvas.set_input_enabled(True)
        self.canvas.keyPressEvent(self.key(QEvent.Type.KeyPress, Qt.Key.Key_A, text="a"))
        self.canvas.keyReleaseEvent(self.key(QEvent.Type.KeyRelease, Qt.Key.Key_A, text="a"))
        self.assertEqual(self.events[:2], [
            {"type": "key", "key": "a", "down": True},
            {"type": "key", "key": "a", "down": False},
        ])
        self.canvas.keyPressEvent(self.key(QEvent.Type.KeyPress, Qt.Key.Key_Shift))
        self.canvas.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
        self.assertEqual(self.events[-1], {"type": "release_all"})
        self.assertFalse(self.canvas._pressed_keys)

    def test_emergency_shortcut_is_not_remote_escape(self):
        modifiers = (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                     | Qt.KeyboardModifier.ShiftModifier)
        self.canvas.keyPressEvent(self.key(QEvent.Type.KeyPress, Qt.Key.Key_Escape, modifiers))
        self.assertEqual(self.emergencies, [True])
        self.assertEqual(self.events, [])

    def test_shifted_punctuation_normalizes_to_base_keys(self):
        for typed, base in (("!", "1"), ("+", "="), (":", ";"), ('"', "'"),
                            ("?", "/"), ("|", "\\"), ("{", "["), ("~", "`")):
            event = self.key(QEvent.Type.KeyPress, ord(typed), Qt.KeyboardModifier.ShiftModifier, typed)
            self.assertEqual(normalized_key(event), base)

    def test_russian_layout_uses_source_physical_key_codes(self):
        event = QKeyEvent(QEvent.Type.KeyPress, ord("А"), Qt.KeyboardModifier.NoModifier,
                          0, 0x46, 0, "а")
        with mock.patch("virtualkvm.gui.sys.platform", "win32"):
            self.assertEqual(normalized_key(event), "f")
        event = QKeyEvent(QEvent.Type.KeyPress, ord("Ф"), Qt.KeyboardModifier.NoModifier,
                          0, 0, 0, "ф")
        with mock.patch("virtualkvm.gui.sys.platform", "darwin"):
            self.assertEqual(normalized_key(event), "a")

    def test_event_loop_wait_allows_python_worker_cleanup(self):
        stop = threading.Event()
        ticks = []

        def cleanup_worker():
            while not stop.wait(0.005):
                ticks.append(True)

        worker = threading.Thread(target=cleanup_worker, daemon=True)
        worker.start()
        try:
            wait_ms(200)
            # A GIL-retaining qWait allows only a post-return tick. Multiple
            # worker turns prove cleanup can progress inside the event loop.
            self.assertGreaterEqual(len(ticks), 3)
        finally:
            stop.set()
            worker.join(timeout=1)


class GuiControllerIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def wait_for(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            wait_ms(20)
        self.assertTrue(predicate(), self.window.log.toPlainText())

    def test_gui_construction_never_resolves_machine_hostname(self):
        with tempfile.TemporaryDirectory(prefix="virtualkvm-gui-no-dns-") as temporary:
            with mock.patch("socket.gethostname", side_effect=AssertionError("GUI must enumerate interfaces without DNS")), \
                    mock.patch("socket.getaddrinfo", side_effect=AssertionError("Synchronous DNS must not run on the GUI thread")):
                self.window = MainWindow(Path(temporary))
                self.window.show()
            self.assertTrue(self.window.host_start.isEnabled())
            self.window.close()
            self.wait_for(lambda: self.window._shutdown_complete)

    def test_profiles_custom_native_preferences_and_secret_exclusion(self):
        import json
        with tempfile.TemporaryDirectory(prefix="virtualkvm-gui-settings-") as temporary:
            state = Path(temporary)
            self.window = MainWindow(state)
            window = self.window
            window.show()
            window.share_enabled.setChecked(True)
            window._start_host()
            self.assertIsNone(window._host, "A blank folder must not expose the current directory")
            window.profile.setCurrentIndex(1)
            self.assertEqual(window.frame_rate.value(), 60)
            window.profile.setCurrentIndex(2)
            self.assertEqual((window.video_width.value(), window.video_height.value()), (3840, 2160))
            window.video_width.setValue(1003)
            window.video_height.setValue(601)
            window.frame_rate.setValue(144)
            self.assertEqual(window.profile.currentIndex(), 3)
            self.assertEqual(window.resolution_preset.currentText(), "Свой размер…")
            window.native_resolution.setChecked(True)
            self.assertFalse(window.video_width.isEnabled())
            window.native_resolution.setChecked(False)
            self.assertTrue(window.video_width.isEnabled())
            window.codec.setCurrentIndex(1)
            self.assertIn("libx265", [window.encoder.itemText(i) for i in range(window.encoder.count())])
            window.peer_pin.setText("not-a-saved-secret")
            window.peer_fingerprint.setText("a" * 64)
            original_host_pin = window.host_pin.text()
            window.shared_path.setText(str(state))
            window.shared_writable.setChecked(True)
            window._save_preferences()
            stored = (state / "preferences.json").read_text()
            self.assertNotIn("not-a-saved-secret", stored)
            self.assertNotIn("a" * 64, stored)
            self.assertNotIn(original_host_pin, stored)
            self.assertNotIn("shared_writable", json.loads(stored))
            window.close()
            self.wait_for(lambda: window._shutdown_complete)
            self.window = MainWindow(state)
            restored = self.window
            self.assertEqual((restored.video_width.value(), restored.video_height.value(), restored.frame_rate.value()),
                             (1003, 601, 144))
            self.assertEqual(restored.codec.currentData(), "hevc")
            self.assertFalse(restored.peer_pin.text())
            self.assertFalse(restored.peer_fingerprint.text())
            self.assertNotEqual(restored.host_pin.text(), original_host_pin)
            self.assertFalse(restored.share_enabled.isChecked())
            self.assertFalse(restored.shared_writable.isChecked())
            restored.close()
            self.wait_for(lambda: restored._shutdown_complete)

    def test_small_desktop_keeps_controls_reachable_with_scroll(self):
        with tempfile.TemporaryDirectory(prefix="virtualkvm-gui-small-") as temporary:
            self.window = MainWindow(Path(temporary))
            window = self.window
            window.resize(1024, 640)
            window.show()
            wait_ms(80)
            self.assertLessEqual(window.minimumHeight(), 600)
            self.assertGreater(window._scroll.verticalScrollBar().maximum(), 0)
            window._scroll.ensureWidgetVisible(window.disconnect_button)
            wait_ms(30)
            center = window.disconnect_button.mapTo(window._scroll.viewport(), window.disconnect_button.rect().center())
            self.assertTrue(window._scroll.viewport().rect().contains(center))
            self.assertGreater(window._scroll.verticalScrollBar().value(), 0)
            window.close()
            self.wait_for(lambda: window._shutdown_complete)

    def test_player_teardown_waits_for_background_relay_disconnect(self):
        for action in ("disconnect", "close"):
            with self.subTest(action=action), tempfile.TemporaryDirectory(prefix="virtualkvm-gui-stop-") as temporary:
                self.window = MainWindow(Path(temporary))
                window = self.window
                window._frame_timer.stop()
                window._stats_timer.stop()
                entered, allow_finish, network_stopped = threading.Event(), threading.Event(), threading.Event()
                qt_closes = []

                class Viewer:
                    connected = True
                    def disconnect(self):
                        entered.set()
                        if not allow_finish.wait(3):
                            raise TimeoutError("test relay disconnect was not released")
                        self.connected = False
                        network_stopped.set()

                class Player:
                    def stop(self):
                        raise AssertionError("Finalization must unload the source, not retain stopped HTTP media")
                    def setSource(self, source):
                        qt_closes.append((network_stopped.is_set(), source.isEmpty()))

                video = VideoWindow(0, lambda event: None, lambda: None, lambda enabled: None, False)
                video._player = Player()
                window._viewer = Viewer()
                window.video_windows[0] = video
                window.show()
                try:
                    window._disconnect_viewer() if action == "disconnect" else window.close()
                    self.wait_for(entered.is_set)
                    # Event processing stays responsive while network teardown
                    # is blocked; no synchronous Qt player stop has run yet.
                    wait_ms(50)
                    self.assertEqual(qt_closes, [])
                    allow_finish.set()
                    if action == "disconnect":
                        self.wait_for(lambda: window._viewer is None)
                    else:
                        self.wait_for(lambda: window._shutdown_complete)
                    self.assertEqual(qt_closes, [(True, True)])
                finally:
                    allow_finish.set()
                    window.close()
                    self.wait_for(lambda: window._shutdown_complete)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for AV integration")
    def test_av_shared_clock_odd_resolution_and_hiding_primary_keeps_secondary(self):
        from virtualkvm.app import HostController
        def phase(name):
            # Native CI retains these markers even when a Qt backend emits a
            # large error storm; no peer identity or credentials are included.
            print(f"GUI_AV_PHASE: {name}", flush=True)

        phase("begin")
        with tempfile.TemporaryDirectory(prefix="virtualkvm-gui-av-") as temporary:
            root = Path(temporary)
            def host_factory(*args, **kwargs):
                kwargs["receive_dir"] = root / "received"
                return HostController(*args, **kwargs)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            self.window = MainWindow(root / "state")
            window = self.window
            window.show()
            try:
                window.synthetic.setChecked(True)
                window.host_bind.setText("127.0.0.1")
                window.host_port.setValue(port)
                with mock.patch("virtualkvm.app.HostController", side_effect=host_factory):
                    window._start_host()
                self.wait_for(lambda: window._host_running)
                phase("host-ready")
                window.peer_host.setText("127.0.0.1")
                window.peer_port.setValue(port)
                window.peer_pin.setText(window.host_pin.text())
                window.peer_fingerprint.setText(window.host_fingerprint.text())
                window.video_width.setValue(320)
                window.video_height.setValue(181)
                window.preserve_aspect.setChecked(False)
                window.frame_rate.setValue(240)
                window.encoder.setCurrentText("libx264")
                window.monitor_layout.set_count(2)
                window.audio_enabled.setChecked(True)
                window._connect_viewer()
                phase("connecting")
                self.wait_for(lambda: len(window.video_windows) == 2, timeout=30)
                phase("two-video-windows-ready")
                self.wait_for(lambda: window.video_windows.get(0) is not None
                    and window.video_windows[0].av_frames >= 4, timeout=30)
                primary = window.video_windows[0]
                phase("primary-frames-decoded")
                self.assertTrue(primary._player.hasAudio())
                self.assertTrue(primary._player.hasVideo())
                self.assertEqual(primary.canvas._media_size, (320, 182))
                primary.mute_action.setChecked(True)
                self.assertTrue(primary._audio_output.isMuted())
                phase("primary-muted")
                primary.close()
                phase("primary-hidden")
                wait_ms(600)
                self.assertIn(0, window._hidden_video_windows)
                self.assertIn(1, window.video_windows)
                self.assertTrue(window._viewer.connected)
                phase("secondary-survived-primary-hide")
                window.video_windows[1].close()
                phase("secondary-closed-awaiting-disconnect")
                self.wait_for(lambda: window._viewer is None)
                phase("viewer-disconnected")
                self.assertFalse(window._hidden_video_windows)
            finally:
                phase("main-window-closing")
                window.close()
                self.wait_for(lambda: window._shutdown_complete, timeout=25)
                phase("shutdown-complete")
                window.deleteLater()
                self.app.processEvents()
        phase("test-complete")

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for real video integration")
    def test_real_pairing_two_video_windows_input_and_clean_shutdown(self):
        from virtualkvm.app import HostController
        with tempfile.TemporaryDirectory(prefix="virtualkvm-gui-") as temporary:
            root = Path(temporary)
            # Keep test file reception entirely within the temporary directory.
            def host_factory(*args, **kwargs):
                kwargs["receive_dir"] = root / "received"
                return HostController(*args, **kwargs)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            self.window = MainWindow(root / "state")
            window = self.window
            window.show()
            try:
                self.assertFalse(window.clipboard_enabled.isChecked())
                self.assertFalse(window.control_enabled.isChecked())
                window.synthetic.setChecked(True)
                window.host_bind.setText("127.0.0.1")
                window.host_port.setValue(port)
                with mock.patch("virtualkvm.app.HostController", side_effect=host_factory):
                    window._start_host()
                self.wait_for(lambda: window._host_running)
                host = window._host
                window.peer_host.setText("127.0.0.1")
                window.peer_port.setValue(port)
                window.peer_pin.setText(window.host_pin.text())
                window.peer_fingerprint.setText(window.host_fingerprint.text())
                window.video_width.setValue(320)
                window.video_height.setValue(180)
                window.frame_rate.setValue(240)
                window.bitrate.setValue(5)
                window.encoder.setCurrentText("libx264")
                window.monitor_layout.set_count(2)
                window._connect_viewer()
                self.wait_for(lambda: len(window.video_windows) == 2, timeout=25)
                self.wait_for(lambda: not window._connecting)
                self.wait_for(lambda: all(not video.canvas._image.isNull() for video in window.video_windows.values()), timeout=25)
                self.assertTrue(window._viewer.connected)
                self.assertEqual(set(window.video_windows), {0, 1})
                for video in window.video_windows.values():
                    self.assertEqual((video.canvas._image.width(), video.canvas._image.height()), (320, 180))
                self.assertEqual(len(window._viewer.receivers), 2)
                window.control_enabled.setChecked(True)
                window.video_windows[0].canvas.keyPressEvent(
                    QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, "a"))
                self.wait_for(lambda: any(e.get("key") == "a" and e.get("down") for e in host.native.events))
                window.emergency_release()
                self.wait_for(lambda: any(e.get("key") == "a" and e.get("down") is False
                                         for e in host.native.events))
                self.assertFalse(window.control_enabled.isChecked())
                window._disconnect_viewer()
                self.wait_for(lambda: window._viewer is None)
                self.assertFalse(window.video_windows)
                self.assertFalse(window.control_enabled.isEnabled())
                self.assertFalse(window.send_file_button.isEnabled())
            finally:
                window.close()
                self.wait_for(lambda: window._shutdown_complete, timeout=25)
                window.deleteLater()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
