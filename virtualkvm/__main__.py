"""Desktop launcher, diagnostics and end-to-end readiness check."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import secrets
import queue
import signal
import sys
import tempfile
import threading
import time


def smoke(fps=240, dual=True, audio=False, transport="srt"):
    from .app import HostController, ViewerController
    from .media import StreamConfig
    players, media_ready = [], queue.Queue()
    application = None
    if audio:
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            os.environ.setdefault("QT_AUDIO_BACKEND", "pulseaudio")
        from PySide6.QtWidgets import QApplication
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
        from PySide6.QtCore import QUrl, QEventLoop, QTimer
        application = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="virtualkvm-smoke-") as temporary:
        root = Path(temporary)
        shared = root / "shared"
        shared.mkdir()
        (shared / "welcome.txt").write_text("VirtualKVM shared folder", encoding="utf-8")
        frames, clipboard, host_clipboard, statuses = {}, [], [], []
        lock = threading.Lock()

        def frame(index, rgb, width, height):
            if len(rgb) != width * height * 3:
                raise RuntimeError("Decoded RGB frame has the wrong length")
            with lock:
                frames[index] = frames.get(index, 0) + 1

        host = HostController(root / "host", bind="127.0.0.1", port=0,
            synthetic=True, on_status=statuses.append,
            on_clipboard=host_clipboard.append, receive_dir=root / "received",
            shared_dir=shared, shared_writable=True)
        viewer = ViewerController(root / "viewer", on_frame=frame,
            on_status=statuses.append, on_clipboard=clipboard.append,
            on_media=lambda *args: media_ready.put(args))
        selected = [0, 1] if dual else [0]
        try:
            host.start()
            viewer.connect("127.0.0.1", host.server.address[1], host.pin,
                host.fingerprint, selected,
                dict(asdict(StreamConfig(width=320, height=180, fps=fps,
                                    encoder="libx264", bitrate_mbps=4, audio=audio)), transport=transport))
            viewer.send_event({"type": "key", "key": "a", "down": True})
            viewer.send_event({"type": "key", "key": "a", "down": False})
            viewer.send_event({"type": "pointer", "x": 0.25, "y": 0.75, "display": selected[-1]})
            viewer.send_clipboard("VirtualKVM → host")
            host.broadcast_clipboard("VirtualKVM → viewer")
            payload = root / "payload.bin"
            payload.write_bytes(bytes(range(256)) * 1024)
            viewer.send_file(payload)
            listing = viewer.shared_list()
            viewer.shared_download("welcome.txt", root / "download.txt")
            if (root / "download.txt").read_text(encoding="utf-8") != "VirtualKVM shared folder":
                raise RuntimeError("Shared folder download differs from source")
            viewer.shared_mkdir("uploads")
            viewer.shared_upload(payload, "uploads/payload.bin")
            if (shared / "uploads" / "payload.bin").read_bytes() != payload.read_bytes():
                raise RuntimeError("Shared folder upload differs from source")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if application:
                    while not media_ready.empty():
                        index, url, width, height = media_ready.get_nowait()
                        player, output, sink = QMediaPlayer(), QAudioOutput(), QVideoSink()
                        output.setMuted(True)  # Readiness checks never play sound through user speakers.
                        player.setAudioOutput(output)
                        player.setVideoOutput(sink)
                        def decoded(video_frame, i=index):
                            if video_frame.isValid():
                                with lock:
                                    frames[i] = frames.get(i, 0) + 1
                        sink.videoFrameChanged.connect(decoded)
                        player.setSource(QUrl(url))
                        player.play()
                        players.append((player, output, sink))
                    event_loop = QEventLoop()
                    QTimer.singleShot(20, event_loop.quit)
                    event_loop.exec()
                with lock:
                    complete = all(frames.get(i, 0) >= 8 for i in selected)
                if complete and clipboard and host_clipboard and len(host.native.events) >= 3:
                    break
                if not application:
                    time.sleep(0.02)
            else:
                details = [s.last_error for s in host.senders] + [r.last_error for r in viewer.receivers]
                raise RuntimeError(f"End-to-end check timed out: frames={frames}; media={details}; status={statuses[-5:]}")
            if (root / "received" / "payload.bin").read_bytes() != payload.read_bytes():
                raise RuntimeError("Received file differs from source")
            if clipboard[-1] != "VirtualKVM → viewer" or host_clipboard[-1] != "VirtualKVM → host":
                raise RuntimeError("Clipboard did not round-trip")
            return dict(passed=True, displays=len(selected), decoded_frames=dict(frames),
                configured_fps=fps, resolution="320x180", input="validated dry-run", transport=transport,
                clipboard="bidirectional", file_bytes=payload.stat().st_size,
                shared_folder="list, download, mkdir, upload verified",
                audio="Qt shared-clock playback (muted check)" if audio else "disabled",
                note="Synthetic integration check; native capture/display FPS and latency require hardware measurement")
        except Exception as error:
            media_errors = [sender.last_error for sender in host.senders] + [receiver.last_error for receiver in viewer.receivers]
            raise RuntimeError(
                f"End-to-end check failed: {type(error).__name__}: {error}; "
                f"media={media_errors}; status={statuses[-8:]}"
            ) from error
        finally:
            viewer.disconnect()
            host.stop()
            for player, output, sink in players:
                player.setSource(QUrl())
            if application:
                event_loop = QEventLoop()
                QTimer.singleShot(30, event_loop.quit)
                event_loop.exec()


def discovery_check():
    """Exercise two real mDNS peers on loopback, including frozen extensions."""
    from .discovery import DiscoveryService
    first, second = [], []
    kind = "_vkvm-check._tcp.local."
    peers = [DiscoveryService(secrets.token_hex(16), name, ["127.0.0.1"], port, "00" * 32, callback,
                              interfaces=["127.0.0.1"], service_type=kind)
             for name, port, callback in (("Check A", 41001, first.append), ("Check B", 41002, second.append))]
    try:
        for peer in peers:
            peer.start()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if first and second and any(d.identifier == peers[1].identifier for d in first[-1]) and any(
                    d.identifier == peers[0].identifier for d in second[-1]):
                return {"mdns_loopback": True, "two_peers": True, "secrets_advertised": False}
            time.sleep(0.03)
        raise RuntimeError("mDNS peers did not discover one another on loopback")
    finally:
        for peer in peers:
            peer.stop()


def ui_check():
    """Render the installed interface and resources without starting sharing."""
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication, QLineEdit, QSpinBox
    from .gui import MainWindow
    application = QApplication.instance() or QApplication([])
    application.setQuitOnLastWindowClosed(True)
    icon = Path(__file__).parent / "assets/check.svg"
    if QImage(str(icon)).isNull():
        raise RuntimeError("The installed interface resource is missing or unreadable")
    with tempfile.TemporaryDirectory(prefix="virtualkvm-ui-check-") as temporary:
        window = MainWindow(Path(temporary))
        window.show()
        application.processEvents()
        for field in window.findChildren(QSpinBox):
            if field.isVisible():
                editor = field.findChild(QLineEdit)
                if editor is None or editor.width() < editor.fontMetrics().horizontalAdvance(field.text()) + 16:
                    raise RuntimeError("The installed numeric controls cannot display their values")
        QTimer.singleShot(200, window.close)
        QTimer.singleShot(10000, application.quit)
        application.exec()
        if not window._shutdown_complete:
            raise RuntimeError("The installed interface did not shut down cleanly")
    return {"interface": True, "resources": True, "clean_shutdown": True}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="virtualkvm", description="Encrypted LAN desktop video and input sharing")
    from . import __version__
    parser.add_argument("--version", action="version", version=f"VirtualKVM {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("doctor", help="Check FFmpeg/SRT, native input and capture permissions")
    commands.add_parser("ui-check", help="Render and close the interface using temporary settings")
    commands.add_parser("discovery-check", help="Verify two mDNS peers on loopback without sharing screens")
    check = commands.add_parser("smoke", help="Run real encrypted synthetic end-to-end check")
    check.add_argument("--fps", type=int, default=240)
    check.add_argument("--single", action="store_true")
    check.add_argument("--audio", action="store_true", help="Also verify synchronized Qt audio/video playback (muted)")
    check.add_argument("--transport", choices=("tcp", "srt"), default="tcp")
    server = commands.add_parser("host", help="Start a headless host")
    server.add_argument("--bind", default="0.0.0.0")
    server.add_argument("--port", type=int, default=24800)
    server.add_argument("--state-dir", type=Path)
    server.add_argument("--receive-dir", type=Path)
    server.add_argument("--synthetic", action="store_true")
    server.add_argument("--audio-device", default="", help="Host FFmpeg capture audio device (system loopback where available)")
    server.add_argument("--share-dir", type=Path, help="Explicit folder to share; defaults to read only")
    server.add_argument("--share-write", action="store_true", help="Allow uploads and directory creation in the chosen shared folder")
    args = parser.parse_args(argv)
    try:
        if args.command == "discovery-check":
            print(json.dumps(discovery_check(), indent=2))
            return 0
        if args.command == "ui-check":
            print(json.dumps(ui_check(), indent=2))
            return 0
        if args.command == "doctor":
            import zeroconf
            from .media import FFmpegLocator
            from .native import NativeInput, list_displays
            report = {"media": FFmpegLocator().check_ready(),
                      "discovery": {"backend": "Bonjour/mDNS", "library_loaded": bool(zeroconf.Zeroconf)},
                      "permissions": NativeInput().permission_status(),
                      "displays": [asdict(d) for d in list_displays()]}
            print(json.dumps(report, indent=2))
            return 0
        if args.command == "smoke":
            print(json.dumps(smoke(args.fps, not args.single, args.audio, args.transport), indent=2))
            return 0
        if args.command == "host":
            from .app import HostController
            if sys.platform not in ("win32", "darwin") and not args.synthetic:
                raise RuntimeError("Native capture requires Windows or macOS; use --synthetic for a Linux demonstration")
            stopped = threading.Event()
            for kind in (signal.SIGINT, signal.SIGTERM):
                signal.signal(kind, lambda *_: stopped.set())
            host = HostController(args.state_dir, args.bind, args.port,
                secrets.token_urlsafe(12), synthetic=args.synthetic,
                on_status=lambda text: print(text, flush=True), receive_dir=args.receive_dir,
                audio_device=args.audio_device, shared_dir=args.share_dir, shared_writable=args.share_write)
            try:
                host.start()
                print(f"Host: {host.server.address[0]}:{host.server.address[1]}", flush=True)
                print(f"SHA-256 fingerprint: {host.fingerprint}", flush=True)
                print(f"Pairing PIN (share privately): {host.pin}", flush=True)
                stopped.wait()
            finally:
                host.stop()
            return 0
        from .gui import run_gui
        return run_gui()
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"VirtualKVM: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
