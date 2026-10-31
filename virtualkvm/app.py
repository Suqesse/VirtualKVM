"""Application orchestration shared by the desktop interface and CLI."""
from __future__ import annotations

from dataclasses import asdict, fields, replace
import ipaddress
import os
from pathlib import Path
import secrets
import socket
import threading

from .control import HostServer, ViewerClient
from .media import AVReceiver, CaptureSource, StreamConfig, VideoReceiver, VideoSender
from .native import NativeInput, list_displays
from .display_layout import MAX_STREAMS, display_inventory


def default_state_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VirtualKVM"
    return Path.home() / ".virtualkvm"


class HostController:
    def __init__(self, state_dir=None, bind="0.0.0.0", port=24800, pin=None,
                 synthetic=False, on_status=None, on_clipboard=None, receive_dir=None,
                 audio_device="", shared_dir=None, shared_writable=False, allow_port_fallback=False):
        self.state_dir = Path(state_dir or default_state_dir())
        self.synthetic = synthetic
        self.audio_device = audio_device
        self.pin = pin or secrets.token_urlsafe(12)
        self.on_status = on_status or (lambda _: None)
        self.native = NativeInput(dry_run=synthetic)
        # A synthetic desktop can always exercise two independent streams.
        if synthetic:
            self.displays = [dict(index=i, name=f"Synthetic display {i + 1}",
                                  x=i * 1920, y=0, width=1920, height=1080) for i in range(2)]
        else:
            self.displays = [asdict(d) for d in list_displays()]
        self._lock = threading.RLock()
        self._pending = []
        self.senders = []
        self._generation = 0
        self._worker = None
        self._monitor_stop = threading.Event()
        self._monitor = None
        self.server = HostServer(bind, port, state_dir=self.state_dir, pin=self.pin,
            on_event=self._event, on_clipboard=on_clipboard,
            receive_dir=receive_dir or Path.home() / "Downloads" / "VirtualKVM",
            on_status=self.on_status, on_disconnect=self._stop_streams,
            displays=self.displays, on_stream_request=self._prepare_streams,
            on_stream_start=self._start_streams, on_stream_stop=self._stop_streams,
            shared_dir=shared_dir, shared_writable=shared_writable,
            allow_port_fallback=allow_port_fallback)

    @property
    def fingerprint(self):
        return self.server.fingerprint

    def start(self):
        self.server.start()
        self._monitor_stop.clear()
        self._monitor = threading.Thread(target=self._monitor_media, name="virtualkvm-host-health", daemon=True)
        self._monitor.start()

    def stop(self):
        self._monitor_stop.set()
        try:
            self.server.stop()
        finally:
            self._stop_streams()
        if self._monitor and self._monitor is not threading.current_thread():
            self._monitor.join(timeout=2)
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=4)

    def _monitor_media(self):
        while not self._monitor_stop.wait(0.25):
            with self._lock:
                if self._worker and self._worker.is_alive():
                    continue
                failed = next((s for s in self.senders if not s.running), None)
            if failed:
                message = "Video failed: " + (failed.last_error or "Capture process exited")
                try:
                    self.on_status(message)
                    self.server.broadcast_status(message)
                except OSError:
                    pass
                finally:
                    self._stop_streams()

    def _event(self, event):
        if isinstance(event, dict) and event.get("type") == "release_all":
            self.native.release_all()
        else:
            self.native.inject(event)

    def _prepare_streams(self, request):
        indexes, options = request.get("displays"), request.get("config")
        if (not isinstance(indexes, list) or not 1 <= len(indexes) <= MAX_STREAMS
                or any(type(i) is not int for i in indexes) or len(set(indexes)) != len(indexes)):
            raise ValueError("Select one to eight distinct displays")
        if not isinstance(options, dict):
            raise ValueError("Invalid stream configuration")
        allowed = {f.name for f in fields(StreamConfig)}
        if set(options) - allowed - {"receiver_ports", "source_resolution", "preserve_aspect"}:
            raise ValueError("Unknown stream setting")
        config = StreamConfig(**{k: v for k, v in options.items() if k in allowed})
        source_resolution = options.get("source_resolution", False)
        if type(source_resolution) is not bool:
            raise ValueError("source_resolution must be a boolean")
        preserve_aspect = options.get("preserve_aspect", False)
        if type(preserve_aspect) is not bool:
            raise ValueError("preserve_aspect must be a boolean")
        if config.audio and not self.synthetic and not self.audio_device:
            raise ValueError("Choose a host audio capture device before enabling sound")
        destination = str(ipaddress.ip_address(request["_peer_host"]))
        ports = options.get("receiver_ports", {})
        available = {d["index"]: d for d in self.displays}
        pending, streams = [], []
        for index in indexes:
            if index not in available:
                raise ValueError(f"Display {index} is unavailable")
            port = ports.get(str(index))
            if type(port) is not int or not 1024 <= port <= 65535:
                raise ValueError("Invalid viewer UDP port")
            if any(s["port"] == port for s in streams):
                raise ValueError("Each display needs a distinct UDP port")
            display = available[index]
            width, height = config.width, config.height
            source_width = display.get("pixel_width") or display["width"]
            source_height = display.get("pixel_height") or display["height"]
            if source_resolution:
                width, height = source_width, source_height
            elif preserve_aspect:
                ratio = min(width / source_width, height / source_height)
                width, height = max(1, round(source_width * ratio)), max(1, round(source_height * ratio))
            stream_config = replace(config,
                width=width, height=height,
                audio=config.audio and index == indexes[0], audio_device=self.audio_device)
            source = CaptureSource(index=index, x=display["x"], y=display["y"],
                                   width=display["width"], height=display["height"])
            key = secrets.token_hex(24)
            pending.append((destination, port, key, source, stream_config))
            streams.append(dict(display=index, port=port, passphrase=key, config=asdict(stream_config)))
        self._stop_streams()
        with self._lock:
            self._pending = pending
        return {"streams": streams, "displays": self.displays}

    def _start_streams(self):
        with self._lock:
            if self._worker and self._worker.is_alive():
                raise RuntimeError("Streams are already starting")
            if self.senders or not self._pending:
                raise RuntimeError("Negotiate streams before starting")
            generation = self._generation
            pending = list(self._pending)

        def start():
            try:
                for destination, port, key, source, config in pending:
                    sender = VideoSender()
                    with self._lock:
                        if generation != self._generation:
                            return
                        self.senders.append(sender)
                    sender.start(destination, port, key, source, config, synthetic=self.synthetic)
                    with self._lock:
                        if generation != self._generation:
                            sender.stop()
                            return
                    for line in sender.diagnostics:
                        self.on_status(line)
                self.on_status("Video streams started")
            except Exception as error:
                self.on_status(f"Video failed: {error}")
                self._stop_streams()

        self._worker = threading.Thread(target=start, name="virtualkvm-stream-start", daemon=True)
        self._worker.start()
        return {"starting": True}

    def _stop_streams(self):
        with self._lock:
            self._generation += 1
            senders, self.senders = self.senders, []
            self._pending = []
        try:
            for sender in senders:
                try:
                    sender.stop()
                except Exception:
                    # Continue cleaning other displays and always release keys.
                    continue
        finally:
            self.native.release_all()

    def broadcast_clipboard(self, text):
        self.server.broadcast_clipboard(text)


class ViewerController:
    @staticmethod
    def discover_displays(state_dir, host, port, pin, fingerprint):
        """Authenticate and read the host inventory without starting capture."""
        client = ViewerClient(Path(state_dir))
        try:
            client.connect(host, port, pin, fingerprint)
            display_inventory(client.displays)
            return list(client.displays)
        finally:
            client.disconnect()

    def __init__(self, state_dir=None, on_status=None, on_frame=None, on_clipboard=None, on_media=None):
        self.on_status = on_status or (lambda _: None)
        self.on_frame = on_frame or (lambda *_: None)
        self.on_media = on_media
        self.client = ViewerClient(Path(state_dir or default_state_dir()),
                                   on_clipboard=on_clipboard, on_status=self._status)
        self.receivers = []
        self.displays = []
        self._input_geometry = {}
        self._monitor_stop = threading.Event()
        self._monitor = None

    @property
    def connected(self):
        return self.client.connected

    def _status(self, message):
        self.on_status(message)
        if message.startswith("Video failed:"):
            # Never join the protocol reader from itself or block its RPC path.
            threading.Thread(target=self.disconnect, name="virtualkvm-media-failure", daemon=True).start()
            return
        if not self.client.connected:
            for receiver in list(self.receivers):
                receiver.stop()

    def connect(self, host, port, pin, fingerprint, displays=None, config=None):
        if self.connected or self.receivers:
            self.disconnect()
        indexes = list(displays if displays is not None else [0])
        options = dict(config or asdict(StreamConfig()))
        reservations = []
        try:
            ports = {}
            for index in indexes:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind(("0.0.0.0", 0))
                reservations.append(sock)
                ports[str(index)] = sock.getsockname()[1]
            options["receiver_ports"] = ports
            self.client.connect(host, port, pin, fingerprint)
            if len(indexes) > self.client.capabilities["max_streams"]:
                raise ValueError("Хост поддерживает меньше экранов. Обновите приложение на хосте или сократите выбор.")
            if not self.client.capabilities["preserve_aspect"]:
                options.pop("preserve_aspect", None)
            response = self.client.request_stream(indexes, options)
            self.displays = response.get("displays", [])
            for reservation in reservations:
                reservation.close()
            reservations.clear()
            for stream in response["streams"]:
                stream_config = StreamConfig(**stream["config"])
                if stream_config.audio:
                    self._input_geometry[stream["display"]] = (
                        stream_config.width, stream_config.height,
                        stream_config.coded_width, stream_config.coded_height)
                if stream_config.audio and self.on_media is None:
                    raise ValueError("Audio playback requires the desktop media player")
                receiver = AVReceiver() if stream_config.audio else VideoReceiver()
                self.receivers.append(receiver)
                index = stream["display"]
                if stream_config.audio:
                    receiver.start("0.0.0.0", stream["port"], stream["passphrase"], stream_config,
                        lambda url, i=index, c=stream_config: self.on_media(i, url, c.coded_width, c.coded_height))
                else:
                    receiver.start("0.0.0.0", stream["port"], stream["passphrase"], stream_config,
                        lambda rgb, width, height, i=index: self.on_frame(i, rgb, width, height))
            self.client.start_streams()
            self._monitor_stop = threading.Event()
            self._monitor = threading.Thread(target=self._monitor_media,
                args=(self._monitor_stop,), name="virtualkvm-viewer-health", daemon=True)
            self._monitor.start()
        except BaseException:
            self.disconnect()
            raise
        finally:
            for reservation in reservations:
                reservation.close()

    def disconnect(self):
        self._monitor_stop.set()
        if self.client.connected:
            try:
                self.client.send_event({"type": "release_all"})
            except Exception:
                pass
        self.client.disconnect()
        receivers, self.receivers = self.receivers, []
        self._input_geometry.clear()
        for receiver in receivers:
            receiver.stop()

    def _monitor_media(self, stopped):
        while not stopped.wait(0.25):
            failed = next((r for r in list(self.receivers) if not r.running), None)
            if failed:
                self.on_status("Video failed: " + (failed.last_error or "Decoder process exited"))
                self.disconnect()
                return

    def send_event(self, event):
        if event.get("type") == "pointer" and event.get("display", 0) in self._input_geometry:
            width, height, coded_width, coded_height = self._input_geometry[event.get("display", 0)]
            event = dict(event)
            event["x"] = max(0.0, min(1.0, event["x"] * coded_width / width))
            event["y"] = max(0.0, min(1.0, event["y"] * coded_height / height))
        self.client.send_event(event)

    def send_clipboard(self, text):
        self.client.send_clipboard(text)

    def send_file(self, path):
        return self.client.send_file(Path(path))

    @property
    def shared_info(self):
        return self.client.shared_info

    def shared_list(self, path=""):
        return self.client.shared_list(path)

    def shared_download(self, remote_path, local_path):
        return self.client.shared_download(remote_path, Path(local_path))

    def shared_upload(self, local_path, remote_path):
        return self.client.shared_upload(Path(local_path), remote_path)

    def shared_mkdir(self, path):
        return self.client.shared_mkdir(path)
