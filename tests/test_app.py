import unittest
from dataclasses import asdict
from pathlib import Path
import tempfile
import time
from unittest.mock import Mock

from virtualkvm.__main__ import smoke
from virtualkvm.app import HostController, ViewerController
from virtualkvm.media import StreamConfig


class ApplicationIntegration(unittest.TestCase):
    def test_synthetic_host_never_queries_physical_displays(self):
        with tempfile.TemporaryDirectory() as directory:
            with unittest.mock.patch('virtualkvm.app.list_displays', side_effect=AssertionError('No desktop in CI')):
                host=HostController(Path(directory), synthetic=True)
            self.assertEqual(len(host.displays),2)

    def test_source_geometry_and_single_audio_stream_negotiation(self):
        with tempfile.TemporaryDirectory() as directory:
            host = HostController(Path(directory), synthetic=True)
            host.displays[0].update(width=1280, height=720, pixel_width=2560, pixel_height=1440)
            config = asdict(StreamConfig(width=321, height=181, audio=True))
            config.update(source_resolution=True, receiver_ports={"0": 41001, "1": 41002})
            response = host._prepare_streams({"displays": [0, 1], "config": config, "_peer_host": "127.0.0.1"})
            primary, secondary = response["streams"]
            self.assertEqual((primary["config"]["width"], primary["config"]["height"]), (2560, 1440))
            self.assertTrue(primary["config"]["audio"])
            self.assertFalse(secondary["config"]["audio"])
            host._stop_streams()

    def test_dual_video_control_clipboard_and_file(self):
        result = smoke(fps=240, dual=True)
        self.assertTrue(result["passed"])
        self.assertEqual(result["displays"], 2)
        self.assertEqual(result["file_bytes"], 262144)
        self.assertEqual(set(result["decoded_frames"]), {0, 1})
        self.assertTrue(all(count >= 8 for count in result["decoded_frames"].values()))

    def test_release_survives_media_cleanup_error(self):
        with tempfile.TemporaryDirectory() as directory:
            host = HostController(Path(directory), synthetic=True)
            host.native.inject({"type": "key", "key": "ctrl", "down": True})
            broken = Mock()
            broken.stop.side_effect = RuntimeError("simulated process cleanup failure")
            healthy = Mock()
            host.senders = [broken, healthy]
            host._stop_streams()
            healthy.stop.assert_called_once()
            self.assertEqual(host.native.events[-1], {"type": "key", "key": "ctrl", "down": False})

    def test_failed_video_disconnects_viewer_and_releases_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frames = []
            host = HostController(root / "host", bind="127.0.0.1", port=0, synthetic=True)
            viewer = ViewerController(root / "viewer", on_frame=lambda *args: frames.append(args[0]))
            try:
                host.start()
                viewer.connect("127.0.0.1", host.server.address[1], host.pin, host.fingerprint,
                    [0], asdict(StreamConfig(width=160, height=90, fps=30, encoder="libx264")))
                deadline = time.monotonic() + 10
                while not frames and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(frames)
                viewer.send_event({"type": "key", "key": "shift", "down": True})
                while not host.native.events and time.monotonic() < deadline:
                    time.sleep(0.02)
                host.senders[0].stop()
                while viewer.connected and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(viewer.connected)
                while host.native.events[-1].get("down") is not False and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(host.native.events[-1]["down"])
            finally:
                viewer.disconnect()
                host.stop()


if __name__ == "__main__":
    unittest.main()
