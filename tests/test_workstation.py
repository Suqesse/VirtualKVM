"""Office-workstation path: GPU capture/decode, captured mouse, Windows elevation."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.environ.get("QT_QPA_PLATFORM") in ("offscreen", "minimal"):
    os.environ.setdefault("QT_AUDIO_BACKEND", "pulseaudio")

import secrets
import socket
import threading
import unittest
from unittest import mock
from xml.etree import ElementTree

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from virtualkvm import native, winadmin
from virtualkvm.gui import VideoCanvas, VideoWindow, _InputPump, nv12_rendering_available
from virtualkvm.media import CaptureSource, FFmpegLocator, StreamConfig, VideoReceiver, VideoSender, frame_size


class RelativeInputValidationTests(unittest.TestCase):
    def test_relative_move_is_validated_and_recorded(self):
        device = native.NativeInput(dry_run=True)
        device.inject({"type": "move", "dx": -15, "dy": 4096})
        self.assertEqual(device.events[-1], {"type": "move", "dx": -15, "dy": 4096})
        for bad in ({"type": "move", "dx": 1.5, "dy": 0}, {"type": "move", "dx": True, "dy": 0},
                    {"type": "move", "dx": 4097, "dy": 0}, {"type": "move", "dx": 0, "dy": -4097},
                    {"type": "move", "dx": 0, "dy": 0, "display": 0}):
            with self.subTest(event=bad), self.assertRaises(ValueError):
                device.inject(bad)

    def test_windows_relative_move_uses_plain_mouseeventf_move(self):
        backend = native._WindowsBackend.__new__(native._WindowsBackend)
        sent = []
        backend._send = sent.append
        backend.send({"type": "move", "dx": -7, "dy": 3})
        self.assertEqual((sent[0].mi.dx, sent[0].mi.dy, sent[0].mi.dwFlags), (-7, 3, 1))
        backend.send({"type": "move", "dx": 0, "dy": 0})
        self.assertEqual(len(sent), 1)


class DxgiSelectionTests(unittest.TestCase):
    def test_outputs_match_exact_desktop_rectangles(self):
        outputs = [native.DxgiOutput(0, 0, 0, 0, 2560, 1440), native.DxgiOutput(0, 1, -1080, 0, 1080, 1920, 2)]
        self.assertEqual(native.match_dxgi_output(outputs, 0, 0, 2560, 1440).output, 0)
        self.assertTrue(native.match_dxgi_output(outputs, -1080, 0, 1080, 1920).rotated)
        self.assertIsNone(native.match_dxgi_output(outputs, 0, 0, 1920, 1080))

    def test_non_windows_has_no_dxgi_outputs(self):
        with mock.patch("virtualkvm.native.platform.system", return_value="Linux"):
            self.assertEqual(native.dxgi_outputs(), [])
            self.assertFalse(native.is_windows_admin())


class WindowsCaptureCommandTests(unittest.TestCase):
    """Command construction only; real capture needs a Windows desktop and GPU."""

    def build(self, config, encoder, outputs, *, ddagrab=True, usable=True, env=None):
        sender = VideoSender()
        commands = []
        sender._spawn = lambda command, *args: commands.append(command)
        with mock.patch("virtualkvm.media.platform.system", return_value="Windows"), \
             mock.patch.object(sender.locator, "check_ready"), \
             mock.patch.object(sender, "_encoder", return_value=encoder), \
             mock.patch.object(sender.locator, "filters", return_value=frozenset({"ddagrab"} if ddagrab else set())), \
             mock.patch.object(sender.locator, "ddagrab_usable", return_value=usable), \
             mock.patch("virtualkvm.native.dxgi_outputs", return_value=outputs), \
             mock.patch.dict(os.environ, env or {}, clear=False):
            sender.start("127.0.0.1", 9000, "x" * 32, CaptureSource(0, 0, 0, 1920, 1080), config, transport="tcp")
        return sender, commands[0]

    def test_nvenc_at_native_size_takes_gpu_frames_without_download(self):
        outputs = [native.DxgiOutput(0, 0, 0, 0, 1920, 1080)]
        sender, command = self.build(StreamConfig(width=1920, height=1080, fps=60), "h264_nvenc", outputs)
        self.assertEqual((sender.capture_method, sender.zero_copy), ("ddagrab", True))
        self.assertIn("ddagrab=output_idx=0:framerate=60:draw_mouse=1", command)
        self.assertNotIn("-vf", command)
        self.assertNotIn("-pix_fmt", command)
        self.assertEqual(command[command.index("-preset") + 1], "p4")
        self.assertIn("Capture: ddagrab with zero-copy GPU encoding", sender.diagnostics)

    def test_scaled_or_software_path_downloads_once_then_scales(self):
        outputs = [native.DxgiOutput(0, 0, 0, 0, 1920, 1080)]
        sender, command = self.build(StreamConfig(width=1280, height=720, fps=60), "libx264", outputs)
        self.assertEqual((sender.capture_method, sender.zero_copy), ("ddagrab", False))
        self.assertTrue(command[command.index("-vf") + 1].startswith("hwdownload,format=bgra,scale=1280:720"))
        self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(command[command.index("-preset") + 1], "superfast")

    def test_high_frame_rate_keeps_lowest_latency_nvenc_tuning(self):
        outputs = [native.DxgiOutput(0, 0, 0, 0, 1920, 1080)]
        _sender, command = self.build(StreamConfig(width=1920, height=1080, fps=240), "h264_nvenc", outputs)
        self.assertEqual(command[command.index("-preset") + 1], "p1")
        self.assertEqual(command[command.index("-tune") + 1], "ull")

    def test_falls_back_to_gdi_capture(self):
        outputs = [native.DxgiOutput(0, 0, 0, 0, 1920, 1080)]
        cases = (dict(outputs=[]), dict(outputs=outputs, ddagrab=False), dict(outputs=outputs, usable=False),
                 dict(outputs=[native.DxgiOutput(0, 0, 0, 0, 1920, 1080, 2)]),
                 dict(outputs=[native.DxgiOutput(1, 0, 0, 0, 1920, 1080)]),
                 dict(outputs=outputs, env={"VIRTUALKVM_CAPTURE": "gdigrab"}))
        for case in cases:
            with self.subTest(case=case):
                sender, command = self.build(StreamConfig(width=1920, height=1080, fps=60), "h264_nvenc", **case)
                self.assertEqual(sender.capture_method, "gdigrab")
                self.assertIn("gdigrab", command)
                self.assertFalse(command[command.index("-vf") + 1].startswith("hwdownload"))


class Nv12ReceiverTests(unittest.TestCase):
    def test_frame_sizes(self):
        self.assertEqual(frame_size("nv12", 322, 182), 322 * 182 * 3 // 2)
        self.assertEqual(frame_size("rgb24", 321, 181), 321 * 181 * 3)
        with self.assertRaises(ValueError):
            VideoReceiver(pixel_format="yuv444p")

    def test_hardware_decode_request_falls_back_and_delivers_coded_nv12(self):
        locator = FFmpegLocator()
        if "srt" not in locator.protocols() or not locator.encoder_usable("libx264"):
            self.skipTest("FFmpeg lacks SRT or libx264")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        config = StreamConfig(width=321, height=181, fps=30, bitrate_mbps=2)
        receiver = VideoReceiver(locator, pixel_format="nv12", hwaccel=True)
        sender = VideoSender(locator)
        frames, ready = [], threading.Event()

        def on_frame(data, width, height):
            frames.append((type(data), len(data), width, height))
            ready.set()

        key = secrets.token_hex(16)
        try:
            receiver.start("127.0.0.1", port, key, config, on_frame)
            sender.start("127.0.0.1", port, key, CaptureSource(), config, synthetic=True)
            self.assertTrue(ready.wait(15), (sender.diagnostics, receiver.diagnostics))
            self.assertEqual(frames[0], (bytes, 322 * 182 * 3 // 2, 322, 182))
        finally:
            sender.stop()
            receiver.stop()


class CapturedMouseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.events = []
        self.canvas = VideoCanvas(0, self.events.append, lambda: None)
        self.canvas.resize(640, 360)
        self.canvas.set_frame(bytes(320 * 180 * 3), 320, 180)
        self.canvas.set_input_enabled(True)

    def tearDown(self):
        self.canvas.set_relative(False)
        self.canvas.deleteLater()
        self.app.processEvents()

    def mouse(self, kind, offset_x, offset_y, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.NoButton):
        center = self.canvas.mapToGlobal(self.canvas.rect().center())
        local = QPointF(self.canvas.rect().center()) + QPointF(offset_x, offset_y)
        return QMouseEvent(kind, local, QPointF(center) + QPointF(offset_x, offset_y), button, buttons,
                           Qt.KeyboardModifier.NoModifier)

    def test_captured_mode_sends_relative_motion_and_bare_buttons(self):
        self.assertTrue(self.canvas.set_relative(True))
        self.canvas.mouseMoveEvent(self.mouse(QEvent.Type.MouseMove, 12, -5))
        self.canvas.mouseMoveEvent(self.mouse(QEvent.Type.MouseMove, 0, 0))
        self.canvas.mousePressEvent(self.mouse(QEvent.Type.MouseButtonPress, 0, 0,
                                               Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton))
        self.canvas.mouseReleaseEvent(self.mouse(QEvent.Type.MouseButtonRelease, 0, 0, Qt.MouseButton.LeftButton))
        ratio = self.canvas.devicePixelRatioF()
        self.assertEqual(self.events, [
            {"type": "move", "dx": round(12 * ratio), "dy": round(-5 * ratio)},
            {"type": "button", "button": "left", "down": True},
            {"type": "button", "button": "left", "down": False},
        ])

    def test_capture_requires_control_and_host_support_and_ends_on_focus_loss(self):
        self.canvas.relative_allowed = lambda: False
        self.assertFalse(self.canvas.set_relative(True))
        self.canvas.relative_allowed = lambda: True
        self.canvas.set_input_enabled(False)
        self.assertFalse(self.canvas.set_relative(True))
        self.canvas.set_input_enabled(True)
        self.assertTrue(self.canvas.set_relative(True))
        self.canvas.set_input_enabled(False)
        self.assertFalse(self.canvas.relative)

    def test_hotkey_toggles_capture_and_window_action_follows(self):
        window = VideoWindow(0, self.events.append, lambda: None, lambda _checked: None, True,
                             relative_allowed=lambda: True)
        try:
            modifiers = (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                         | Qt.KeyboardModifier.ShiftModifier)
            window.canvas.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_M, modifiers))
            self.assertTrue(window.canvas.relative)
            self.assertTrue(window.capture_action.isChecked())
            window.canvas.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, modifiers))
            self.assertFalse(window.canvas.relative)
            self.assertFalse(window.capture_action.isChecked())
            self.assertFalse([e for e in self.events if e.get("type") == "key"])
        finally:
            window.canvas.set_relative(False)
            window.deleteLater()

    def test_input_pump_sums_relative_motion(self):
        sent, gate = [], threading.Event()

        def send(event):
            gate.wait(5)
            sent.append(event)

        pump = _InputPump(send, lambda _message: None)
        try:
            pump.submit({"type": "key", "key": "a", "down": True})
            for dx in (3, 4, -2):
                pump.submit({"type": "move", "dx": dx, "dy": 1})
            gate.set()
            deadline = threading.Event()
            for _ in range(50):
                if len(sent) >= 2:
                    break
                deadline.wait(0.05)
            self.assertIn({"type": "move", "dx": 5, "dy": 3}, sent)
        finally:
            pump.stop()


class Nv12PresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_nv12_frame_goes_to_gpu_sink_with_padded_strides(self):
        if not nv12_rendering_available():
            self.skipTest("QtMultimedia cannot present NV12 here")
        canvas = VideoCanvas(0, lambda _event: None, lambda: None)
        try:
            width, height = 322, 182
            luma = bytes(range(256)) * (width * height // 256) + bytes(width * height % 256)
            canvas.set_frame(luma + bytes([128]) * (width * height // 2), width, height)
            self.assertTrue(canvas.has_frame)
            self.assertEqual(canvas.frame_size, (width, height))
            frame = canvas._nv12_widget.videoSink().videoFrame()
            self.assertTrue(frame.isValid())
            from PySide6.QtMultimedia import QVideoFrame
            self.assertTrue(frame.map(QVideoFrame.MapMode.ReadOnly))
            try:
                stride = frame.bytesPerLine(0)
                plane = frame.bits(0)
                self.assertEqual(bytes(plane[stride:stride + width]), luma[width:2 * width])
            finally:
                frame.unmap()
        finally:
            canvas.deleteLater()


class WindowsElevationTests(unittest.TestCase):
    def test_task_definition_is_elevated_interactive_and_escaped(self):
        xml = winadmin.task_xml(r"C:\Program Files\VirtualKVM & Co\VirtualKVM.exe", ["--flag", "a b"], r"PC\Иван")
        root = ElementTree.fromstring(xml.split("\n", 1)[1])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(root.find(".//t:RunLevel", ns).text, "HighestAvailable")
        self.assertEqual(root.find(".//t:LogonType", ns).text, "InteractiveToken")
        self.assertEqual(root.find(".//t:LogonTrigger/t:UserId", ns).text, r"PC\Иван")
        self.assertEqual(root.find(".//t:Command", ns).text, r"C:\Program Files\VirtualKVM & Co\VirtualKVM.exe")
        self.assertEqual(root.find(".//t:Arguments", ns).text, '--flag "a b"')
        with self.assertRaises(ValueError):
            winadmin.task_xml("app.exe\n", [], "user")

    def test_launch_command_for_frozen_and_source_runs(self):
        with mock.patch.object(winadmin.sys, "frozen", True, create=True), \
             mock.patch.object(winadmin.sys, "executable", r"C:\Apps\VirtualKVM.exe"):
            self.assertEqual(winadmin.launch_command(["x"]), (r"C:\Apps\VirtualKVM.exe", ["x"]))
        executable, arguments = winadmin.launch_command()
        self.assertEqual(arguments, ["-m", "virtualkvm"])

    def test_elevation_is_unavailable_outside_windows(self):
        with mock.patch("virtualkvm.winadmin.platform.system", return_value="Linux"):
            self.assertFalse(winadmin.autostart_enabled())
            with self.assertRaises(winadmin.ElevationError):
                winadmin.enable_autostart()
            with self.assertRaises(winadmin.ElevationError):
                winadmin.restart_elevated()


if __name__ == "__main__":
    unittest.main()
