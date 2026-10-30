import secrets
import json
import subprocess
import shutil
import socket
import threading
import time
import unittest
from unittest.mock import patch

from virtualkvm.media import (AVReceiver, CaptureSource, FFmpegLocator, MediaError, StreamConfig,
                              VideoReceiver, VideoSender, _srt_url)


class ConfigurationTests(unittest.TestCase):
    def test_defaults_keep_240_fps_goal(self):
        self.assertEqual(StreamConfig().fps, 240)
        for kwargs in ({"fps": 0}, {"width": 0}, {"bitrate_mbps": float("nan")},
                       {"encoder": "x; touch /tmp/bad"}, {"fps": True}, {"audio": 1},
                       {"audio_device": "device\ninjected"}, {"width": 16384, "height": 16384}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                StreamConfig(**kwargs)

    def test_custom_and_native_dimensions_are_not_reduced(self):
        config = StreamConfig(width=321, height=181)
        self.assertEqual((config.width, config.height), (321, 181))
        self.assertEqual((config.coded_width, config.coded_height), (322, 182))
        self.assertEqual(StreamConfig(width=16384, height=64).width, 16384)
        self.assertEqual(StreamConfig(width=1, height=1).coded_width, 2)

    def test_native_sound_requires_explicit_capture_device(self):
        sender = VideoSender()
        with patch("virtualkvm.media.platform.system", return_value="Windows"), \
             self.assertRaisesRegex(MediaError, "Select an audio capture device"):
            sender._capture_args(CaptureSource(), StreamConfig(audio=True), False)

    def test_windows_audio_capture_shares_wallclock_and_syncs_input(self):
        sender = VideoSender()
        with patch("virtualkvm.media.platform.system", return_value="Windows"):
            args = sender._capture_args(CaptureSource(), StreamConfig(audio=True, audio_device="Stereo Mix"), False)
        self.assertEqual(args.count("-use_wallclock_as_timestamps"), 2)
        self.assertIn("audio=Stereo Mix", args)
        self.assertEqual(args[args.index("-isync") + 1], "0")

    def test_capture_dimensions_and_negative_coordinates(self):
        self.assertEqual(CaptureSource(index=1, x=-1920, y=0, width=1920, height=1080).x, -1920)
        with self.assertRaises(ValueError):
            CaptureSource(width=1920)

    def test_encryption_required_and_url_input_validated(self):
        url = _srt_url("::1", 12000, "secret?&=012345", 30, "caller")
        self.assertIn("srt://[::1]:12000?", url)
        self.assertIn("enforced_encryption=1", url)
        self.assertIn("passphrase=secret%3F%26%3D012345", url)
        for host in ("localhost/a", "peer?injected=1", "a b", ""):
            with self.subTest(host=host), self.assertRaises(ValueError):
                _srt_url(host, 12000, "1234567890", 30, "caller")
        with self.assertRaises(ValueError):
            _srt_url("localhost", 12000, "short", 30, "caller")

    def test_missing_ffmpeg_gives_actionable_error(self):
        with self.assertRaisesRegex(MediaError, "Install FFmpeg"):
            FFmpegLocator(ffmpeg="virtualkvm-nonexistent-ffmpeg")

    def test_mac_capture_maps_screen_to_avfoundation_device(self):
        sender = VideoSender()
        with patch("virtualkvm.media.platform.system", return_value="Darwin"), \
             patch.object(sender.locator, "screen_devices", return_value=[
                 {"device_index": 3}, {"device_index": 7}]):
            args = sender._capture_args(CaptureSource(index=1), StreamConfig(), False)
        self.assertEqual(args[-1], "7:none")

    def test_windows_capture_keeps_monitor_offset(self):
        sender = VideoSender()
        with patch("virtualkvm.media.platform.system", return_value="Windows"):
            args = sender._capture_args(CaptureSource(1, -1920, 0, 1920, 1080), StreamConfig(), False)
        self.assertEqual(args[args.index("-offset_x") + 1], "-1920")
        self.assertIn("1920x1080", args)

    def test_diagnostics_redact_srt_secrets(self):
        sender = VideoSender()
        sender._secrets = ("1234567890",)
        sender._log("Could not open srt://peer?passphrase=1234567890&pbkeylen=16")
        self.assertNotIn("1234567890", "\n".join(sender.diagnostics))
        self.assertIn("[redacted]", sender.diagnostics[0])


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg executable is not installed")
class MediaIntegrationTests(unittest.TestCase):
    def test_audio_video_relay_preserves_synchronized_timestamps(self):
        self._assert_audio_video_relay("h264")

    def test_hevc_audio_relay_preserves_synchronized_timestamps(self):
        self._assert_audio_video_relay("hevc")

    def _assert_audio_video_relay(self, codec):
        locator = FFmpegLocator()
        encoder = "libx264" if codec == "h264" else "libx265"
        if "srt" not in locator.protocols() or encoder not in locator.encoders() or not locator.ffprobe:
            self.skipTest(f"FFmpeg requires SRT, {encoder}, and ffprobe")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        config = StreamConfig(width=321, height=181, fps=60, bitrate_mbps=2, audio=True, codec=codec)
        receiver, sender = AVReceiver(locator), VideoSender(locator)
        ready = threading.Event()
        urls = []
        def on_ready(url):
            urls.append(url)
            ready.set()
        key = secrets.token_hex(16)
        try:
            receiver.start("127.0.0.1", port, key, config, on_ready)
            sender.start("127.0.0.1", port, key, CaptureSource(), config, synthetic=True)
            self.assertTrue(ready.wait(12), (sender.diagnostics, receiver.diagnostics))
            self.assertTrue(urls[0].startswith("http://127.0.0.1:"))
            result = subprocess.run([locator.ffprobe, "-v", "error", "-read_intervals", "%+1",
                "-show_streams", "-show_packets", "-show_entries",
                "stream=index,codec_type,codec_name,width,height,sample_rate,channels:packet=stream_index,pts_time,dts_time,duration_time",
                "-of", "json", urls[0]], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            streams = {stream["codec_type"]: stream for stream in data["streams"]}
            self.assertEqual(set(streams), {"audio", "video"})
            self.assertEqual(streams["video"]["codec_name"], codec)
            self.assertEqual((streams["video"]["width"], streams["video"]["height"]), (322, 182))
            self.assertEqual(streams["audio"]["codec_name"], "aac")
            self.assertEqual(streams["audio"]["sample_rate"], "48000")
            self.assertEqual(streams["audio"]["channels"], 2)
            pts = {}
            for packet in data["packets"]:
                if "pts_time" in packet:
                    pts.setdefault(packet["stream_index"], []).append(float(packet["pts_time"]))
            video = pts[streams["video"]["index"]]
            audio = pts[streams["audio"]["index"]]
            self.assertGreater(len(video), 20)
            self.assertGreater(len(audio), 20)
            self.assertLess(abs(video[0] - audio[0]), 0.1)
            self.assertLess(abs(video[-1] - audio[-1]), 0.15)
            self.assertEqual(video, sorted(video))
            self.assertEqual(audio, sorted(audio))
            self.assertNotIn(key, "\n".join(sender.diagnostics + receiver.diagnostics))
        finally:
            sender.stop()
            receiver.stop()

    def test_odd_custom_resolution_rgb_is_exact_viewport(self):
        locator = FFmpegLocator()
        if "srt" not in locator.protocols() or "libx264" not in locator.encoders():
            self.skipTest("FFmpeg lacks SRT or libx264")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        config = StreamConfig(width=321, height=181, fps=30, bitrate_mbps=2)
        receiver, sender = VideoReceiver(locator), VideoSender(locator)
        frames = []
        ready = threading.Event()
        def on_frame(data, width, height):
            frames.append((len(data), width, height))
            ready.set()
        key = secrets.token_hex(16)
        try:
            receiver.start("127.0.0.1", port, key, config, on_frame)
            sender.start("127.0.0.1", port, key, CaptureSource(), config, synthetic=True)
            self.assertTrue(ready.wait(10), (sender.diagnostics, receiver.diagnostics))
            self.assertEqual(frames[0], (321 * 181 * 3, 321, 181))
        finally:
            sender.stop()
            receiver.stop()

    def test_busy_listener_port_is_reported_and_process_is_cleaned_up(self):
        locator = FFmpegLocator()
        if "srt" not in locator.protocols():
            self.skipTest("FFmpeg lacks SRT")
        receiver = VideoReceiver(locator)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
            with self.assertRaises(MediaError):
                receiver.start("127.0.0.1", port, secrets.token_hex(16),
                               StreamConfig(width=160, height=90), lambda *_: None)
        self.assertFalse(receiver.running)

    def test_wrong_srt_key_never_delivers_a_frame(self):
        locator = FFmpegLocator()
        if "srt" not in locator.protocols() or "libx264" not in locator.encoders():
            self.skipTest("FFmpeg lacks SRT or libx264")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        config = StreamConfig(width=160, height=90, fps=30, bitrate_mbps=2)
        receiver, sender = VideoReceiver(locator), VideoSender(locator)
        delivered = threading.Event()
        try:
            receiver.start("127.0.0.1", port, secrets.token_hex(16), config,
                           lambda *_: delivered.set())
            sender.start("127.0.0.1", port, secrets.token_hex(16), CaptureSource(), config, synthetic=True)
            deadline = time.monotonic() + 8
            while sender.running and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(sender.running, sender.diagnostics)
            self.assertFalse(delivered.is_set(), "A peer with the wrong key must not receive decoded video")
            self.assertIsNotNone(sender.last_error)
        finally:
            sender.stop()
            receiver.stop()

    def test_encrypted_synthetic_240_fps_stream_decodes_and_stops(self):
        locator = FFmpegLocator()
        if "srt" not in locator.protocols() or "libx264" not in locator.encoders():
            self.skipTest("FFmpeg lacks SRT or libx264")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        config = StreamConfig(width=160, height=90, fps=240, bitrate_mbps=2)
        receiver, sender = VideoReceiver(locator), VideoSender(locator)
        enough_frames = threading.Event()
        frame_shapes, frames = [], []

        def on_frame(data, width, height):
            frame_shapes.append((len(data), width, height))
            if len(frames) < 3:
                frames.append(data)
            if len(frame_shapes) >= 60:
                enough_frames.set()

        key = secrets.token_hex(16)
        try:
            receiver.start("127.0.0.1", port, key, config, on_frame)
            time.sleep(0.2)
            sender.start("127.0.0.1", port, key, CaptureSource(), config, synthetic=True)
            self.assertTrue(enough_frames.wait(12), (sender.diagnostics, receiver.diagnostics))
            self.assertTrue(sender.running)
            self.assertTrue(receiver.running)
            self.assertEqual(sender.selected_encoder, "libx264")
            self.assertTrue(all(shape == (160 * 90 * 3, 160, 90) for shape in frame_shapes))
            self.assertGreater(len(set(frames)), 1, "Decoded frames should show an advancing test pattern")
            self.assertNotIn(key, "\n".join(sender.diagnostics + receiver.diagnostics))
        finally:
            sender.stop()
            receiver.stop()
        self.assertFalse(sender.running)
        self.assertFalse(receiver.running)


if __name__ == "__main__":
    unittest.main()
