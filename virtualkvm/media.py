"""FFmpeg-backed screen capture and encrypted SRT video transport.

Packaged applications include a verified native FFmpeg runtime. Source checkouts
can use explicitly selected or locally installed tools. Only RGB video crosses into
Python; codec and transport work stays in FFmpeg. Callbacks run on a worker
thread and should hand off promptly to the UI thread.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import lru_cache
import ipaddress
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Callable
from urllib.parse import quote, urlencode


class MediaError(RuntimeError):
    """Missing capability, invalid media configuration, or process failure."""


@dataclass(frozen=True)
class StreamConfig:
    width: int = 1920
    height: int = 1080
    fps: int = 240
    bitrate_mbps: float = 30
    codec: str = "h264"
    encoder: str = "auto"
    latency_ms: int = 30
    audio: bool = False
    audio_device: str = ""

    def __post_init__(self) -> None:
        for name, value, low, high in (("width", self.width, 1, 16384),
                                      ("height", self.height, 1, 16384),
                                      ("fps", self.fps, 1, 240),
                                      ("latency_ms", self.latency_ms, 10, 2000)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer from {low} to {high}")
        if self.coded_width * self.coded_height > 67108864:
            raise ValueError("Video dimensions exceed the 67,108,864-pixel safety limit")
        if isinstance(self.bitrate_mbps, bool) or not isinstance(self.bitrate_mbps, (int, float)) or not 1 <= self.bitrate_mbps <= 500:
            raise ValueError("bitrate_mbps must be between 1 and 500")
        if self.codec not in {"h264", "hevc"}:
            raise ValueError("codec must be h264 or hevc")
        if not isinstance(self.encoder, str) or not re.fullmatch(r"[a-zA-Z0-9_]+", self.encoder):
            raise ValueError("Invalid encoder name")
        if type(self.audio) is not bool:
            raise ValueError("audio must be a boolean")
        if not isinstance(self.audio_device, str) or len(self.audio_device) > 512 or any(ord(c) < 32 for c in self.audio_device):
            raise ValueError("Invalid audio device identifier")

    @property
    def coded_width(self) -> int:
        return (self.width + 1) // 2 * 2

    @property
    def coded_height(self) -> int:
        return (self.height + 1) // 2 * 2


@dataclass(frozen=True)
class CaptureSource:
    index: int = 0
    x: int = 0
    y: int = 0
    width: int | None = None
    height: int | None = None

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0 or self.index > 63:
            raise ValueError("Screen index must be from 0 to 63")
        if type(self.x) is not int or type(self.y) is not int:
            raise ValueError("Screen offsets must be integers")
        if (self.width is None) != (self.height is None):
            raise ValueError("Capture width and height must be supplied together")
        if self.width is not None and (type(self.width) is not int or type(self.height) is not int or
                                       not 1 <= self.width <= 16384 or not 1 <= self.height <= 16384):
            raise ValueError("Invalid capture dimensions")


def _popen_options() -> dict:
    options = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
    if getattr(sys, "frozen", False):
        # PyInstaller's private Qt libraries must not override the media
        # runtime's own dependency graph in subprocesses.
        environment = dict(os.environ)
        for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
            original = environment.get(name + "_ORIG")
            if original is None:
                environment.pop(name, None)
            else:
                environment[name] = original
        options["env"] = environment
    return options


class FFmpegLocator:
    """Locate tools without downloading executables or changing system state."""

    def __init__(self, ffmpeg: str | None = None, ffprobe: str | None = None):
        explicit_ffmpeg = ffmpeg or os.environ.get("VIRTUALKVM_FFMPEG")
        probe_override = ffprobe or os.environ.get("VIRTUALKVM_FFPROBE")
        bundled = self._bundled_pair() if not explicit_ffmpeg else None
        self.ffmpeg = self._locate(explicit_ffmpeg) if explicit_ffmpeg else (bundled[0] if bundled else self._locate("ffmpeg"))
        if probe_override:
            self.ffprobe = self._locate(probe_override)
        elif bundled:
            self.ffprobe = bundled[1]
        else:
            sibling = os.path.join(os.path.dirname(self.ffmpeg), "ffprobe.exe" if os.name == "nt" else "ffprobe")
            self.ffprobe = self._locate(sibling, required=False) or self._locate("ffprobe")

    @staticmethod
    def _bundle_directories() -> list[Path]:
        executable = Path(sys.executable).resolve()
        candidates = []
        if getattr(sys, "_MEIPASS", None):
            candidates.append(Path(sys._MEIPASS) / "runtime")
        candidates += [executable.parent / "runtime", executable.parent / "_internal" / "runtime",
                       executable.parent.parent / "Resources" / "runtime",
                       Path(__file__).resolve().parent.parent / "runtime"]
        return list(dict.fromkeys(candidates))

    @classmethod
    def _bundled_pair(cls):
        extension = ".exe" if os.name == "nt" else ""
        for directory in cls._bundle_directories():
            ffmpeg, ffprobe = directory / ("ffmpeg" + extension), directory / ("ffprobe" + extension)
            if ffmpeg.exists() or ffprobe.exists():
                tools = (shutil.which(str(ffmpeg)), shutil.which(str(ffprobe)))
                if not all(tools):
                    raise MediaError("The packaged FFmpeg runtime is incomplete. Reinstall VirtualKVM from a complete installer.")
                return tools
        return None

    @staticmethod
    def _locate(command: str, required: bool = True) -> str | None:
        resolved = shutil.which(command)
        if resolved is not None and platform.system() == "Windows" and os.path.basename(command) == command:
            candidate = Path(resolved)
            root = candidate.parent.parent
            configured = os.environ.get("ChocolateyInstall")
            is_shim = candidate.parent.name.lower() == "bin" and (
                root.name.lower() == "chocolatey" or configured and root == Path(configured))
            if is_shim:
                matches = []
                for package in ("ffmpeg-full", "ffmpeg"):
                    location = root / "lib" / package
                    if location.is_dir():
                        matches.extend(p for p in location.rglob(candidate.name) if p.is_file())
                if len(matches) != 1:
                    raise MediaError("Select the real FFmpeg/ffprobe executable from the Chocolatey package tools folder; launcher shims cannot own the SRT listener")
                resolved = str(matches[0].resolve())
        # Finder-launched macOS apps do not inherit a terminal's Homebrew PATH.
        # An explicit path override remains authoritative and is never replaced.
        if resolved is None and platform.system() == "Darwin" and os.path.basename(command) == command:
            for directory in ("/opt/homebrew/bin", "/usr/local/bin"):
                resolved = shutil.which(os.path.join(directory, command))
                if resolved is not None:
                    break
        if resolved is None and required:
            if getattr(sys, "frozen", False):
                raise MediaError(f"{command} was not found in the packaged runtime. Reinstall VirtualKVM from a complete installer.")
            raise MediaError(f"{command} was not found. Install FFmpeg with SRT support for source development and add it to PATH.")
        return resolved

    def _query(self, *args: str) -> str:
        try:
            result = subprocess.run([self.ffmpeg, "-hide_banner", *args],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", timeout=15, **_popen_options())
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaError(f"Could not inspect FFmpeg: {exc}") from exc
        return result.stdout

    @lru_cache(maxsize=16)
    def encoders(self) -> frozenset[str]:
        return frozenset(match.group(1) for line in self._query("-encoders").splitlines()
                         if (match := re.match(r"^\s*V[.A-Z]{5}\s+([a-zA-Z0-9_][a-zA-Z0-9_-]*)\s", line)))

    @lru_cache(maxsize=16)
    def protocols(self) -> frozenset[str]:
        return frozenset(line.strip() for line in self._query("-protocols").splitlines()
                         if re.fullmatch(r"\s+[a-zA-Z0-9_]+", line))

    def check_ready(self) -> dict:
        if "srt" not in self.protocols():
            raise MediaError("This FFmpeg build has no SRT support; install an SRT-enabled build.")
        return {"ffmpeg": self.ffmpeg, "ffprobe": self.ffprobe,
                "srt": True, "encoders": sorted(self.encoders())}

    @lru_cache(maxsize=16)
    def encoder_usable(self, encoder: str, width: int = 128, height: int = 128,
                       fps: int = 30) -> bool:
        """Listed hardware encoders may have no usable device or driver."""
        if encoder not in self.encoders():
            return False
        try:
            result = subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "lavfi", "-i", f"color=size={width}x{height}:rate={fps}", "-frames:v", "2",
                "-an", "-c:v", encoder, "-pix_fmt", "yuv420p", "-bf", "0",
                "-threads", "2", "-f", "null", "-"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3, **_popen_options())
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def screen_devices(self) -> list[dict]:
        """Map logical macOS screen indexes to AVFoundation video device IDs."""
        if platform.system() != "Darwin":
            return []
        output = self._query("-f", "avfoundation", "-list_devices", "true", "-i", "")
        devices = []
        for line in output.splitlines():
            match = re.search(r"\[(\d+)\]\s+(Capture screen\s*(\d+).*)", line)
            if match:
                devices.append({"index": len(devices), "device_index": int(match.group(1)),
                                "name": match.group(2), "capture_index": int(match.group(3))})
        # AVFoundation enumerates CGGetActiveDisplayList's original order;
        # native.list_displays presents the main display first. Match those
        # orders explicitly rather than confusing a display ID/device index.
        if devices:
            try:
                import Quartz
                error, identifiers, count = Quartz.CGGetActiveDisplayList(64, None, None)
                if error:
                    raise MediaError("macOS active screen enumeration failed")
                original = list(identifiers[:count])
                ordered = sorted(original, key=lambda value: value != Quartz.CGMainDisplayID())
                logical = {original.index(identifier): index for index, identifier in enumerate(ordered)}
                for device in devices:
                    device["index"] = logical.get(device["capture_index"], device["capture_index"])
                devices.sort(key=lambda device: device["index"])
            except ImportError as exc:
                raise MediaError("Install pyobjc-framework-Quartz to map macOS display capture devices") from exc
        return devices

    def audio_devices(self) -> list[dict]:
        """Enumerate capture devices; a loopback driver may be needed for system sound."""
        system = platform.system()
        devices = []
        if system == "Windows":
            output = self._query("-f", "dshow", "-list_devices", "true", "-i", "dummy")
            for line in output.splitlines():
                match = re.search(r'"([^"]+)"\s+\(audio\)', line)
                if match:
                    name = match.group(1)
                    devices.append({"index": len(devices), "name": name, "device": name})
        elif system == "Darwin":
            output = self._query("-f", "avfoundation", "-list_devices", "true", "-i", "")
            in_audio = False
            for line in output.splitlines():
                if "AVFoundation audio devices:" in line:
                    in_audio = True
                    continue
                if "AVFoundation video devices:" in line:
                    in_audio = False
                match = re.search(r"\[(\d+)\]\s+(.+)$", line) if in_audio else None
                if match:
                    devices.append({"index": int(match.group(1)), "name": match.group(2), "device": match.group(1)})
        return devices


def _host(host: str) -> str:
    if not isinstance(host, str) or not host or len(host) > 253:
        raise ValueError("A valid peer hostname or IP address is required")
    value = host.removeprefix("[").removesuffix("]")
    try:
        address = ipaddress.ip_address(value)
        return f"[{address}]" if address.version == 6 else str(address)
    except ValueError:
        if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", value):
            raise ValueError("Invalid peer hostname or IP address")
        return value


def _srt_url(host: str, port: int, passphrase: str, latency_ms: int, mode: str) -> str:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("SRT port must be from 1 to 65535")
    if not isinstance(passphrase, str) or not 10 <= len(passphrase.encode("utf-8")) <= 64 or any(ord(c) < 32 for c in passphrase):
        raise ValueError("SRT passphrase must contain 10 to 64 UTF-8 bytes without control characters")
    query = urlencode({"mode": mode, "transtype": "live", "latency": latency_ms * 1000,
        "peerlatency": latency_ms * 1000, "rcvlatency": latency_ms * 1000,
        "passphrase": passphrase, "pbkeylen": 16, "enforced_encryption": 1,
        "connect_timeout": 5000, "timeout": 10000000, "linger": 2,
        "pkt_size": 1316})
    return f"srt://{_host(host)}:{port}?{query}"


def _process_listens_udp(pid: int, port: int) -> bool:
    return _process_owns_listener(pid, port, tcp=False)


def _process_owns_listener(pid: int, port: int, tcp: bool) -> bool:
    """Observe the child's owned socket without connecting or stealing its port."""
    system = platform.system()
    if system == "Linux":
        from pathlib import Path
        directory = Path(f"/proc/{pid}")
        try:
            inodes = set()
            for fd in (directory / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                    if target.startswith("socket:["):
                        inodes.add(target[8:-1])
                except (FileNotFoundError, PermissionError):
                    continue
            protocol = "tcp" if tcp else "udp"
            for table in (directory / f"net/{protocol}", directory / f"net/{protocol}6"):
                if not table.exists():
                    continue
                for line in table.read_text().splitlines()[1:]:
                    fields = line.split()
                    if (len(fields) > 9 and int(fields[1].split(":")[1], 16) == port
                            and fields[9] in inodes and (not tcp or fields[3] == "0A")):
                        return True
            return False
        except (OSError, ValueError) as exc:
            raise MediaError(f"Could not verify the receiver UDP listener: {exc}") from exc
    if system == "Darwin":
        try:
            selector = ["-iTCP", "-sTCP:LISTEN"] if tcp else ["-iUDP"]
            result = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", str(pid), *selector, "-Fn"],
                                    capture_output=True, text=True, timeout=1)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaError(f"Could not verify the receiver UDP listener using lsof: {exc}") from exc
        return any(line.startswith("n") and line.endswith(f":{port}") for line in result.stdout.splitlines())
    if system == "Windows":
        import ctypes
        from ctypes import wintypes
        library = ctypes.WinDLL("iphlpapi")
        get_table = library.GetExtendedTcpTable if tcp else library.GetExtendedUdpTable
        get_table.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                             wintypes.BOOL, wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
        get_table.restype = wintypes.DWORD
        layouts = ((socket.AF_INET, 24, 8, 20), (socket.AF_INET6, 56, 20, 52)) if tcp else (
            (socket.AF_INET, 12, 4, 8), (socket.AF_INET6, 28, 20, 24))
        table_class = 3 if tcp else 1  # TCP_TABLE_OWNER_PID_LISTENER / UDP_TABLE_OWNER_PID
        for family, row_size, port_offset, pid_offset in layouts:
            size = wintypes.DWORD()
            result = get_table(None, ctypes.byref(size), False, family, table_class, 0)
            if result not in (0, 122):  # ERROR_INSUFFICIENT_BUFFER
                continue
            buffer = ctypes.create_string_buffer(size.value)
            result = get_table(buffer, ctypes.byref(size), False, family, table_class, 0)
            if result != 0:
                continue
            data = buffer.raw
            rows = int.from_bytes(data[:4], "little")
            for index in range(rows):
                start = 4 + index * row_size
                if start + row_size > len(data):
                    break
                owner = int.from_bytes(data[start + pid_offset:start + pid_offset + 4], "little")
                local_port = socket.ntohs(int.from_bytes(data[start + port_offset:start + port_offset + 4], "little") & 0xffff)
                if owner == pid and local_port == port:
                    return True
        return False
    raise MediaError(f"Listener readiness is not supported on {system}")


class _FFmpegProcess:
    def __init__(self, locator: FFmpegLocator | None = None):
        self.locator = locator or FFmpegLocator()
        self._process: subprocess.Popen | None = None
        self._diagnostics: deque[str] = deque(maxlen=200)
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.RLock()
        self.last_error: str | None = None
        self._secrets: tuple[str, ...] = ()

    @property
    def running(self) -> bool:
        process = self._process
        if process is None:
            return False
        code = process.poll()
        if code is not None and not self._stop_event.is_set() and self.last_error is None:
            tail = "; ".join(self.diagnostics[-3:])[-1200:]
            self.last_error = f"FFmpeg ended unexpectedly (status {code})" + (f": {tail}" if tail else "")
        return code is None

    @property
    def diagnostics(self) -> list[str]:
        with self._lock:
            return list(self._diagnostics)

    @property
    def log(self) -> list[str]:
        return self.diagnostics

    def _log(self, line: str) -> None:
        for secret in self._secrets:
            line = line.replace(secret, "[redacted]")
        # FFmpeg may echo URLs when a connection or capture device fails.
        line = re.sub(r"(passphrase=)[^&\s'\"]+", r"\1[redacted]", line)
        with self._lock:
            self._diagnostics.append(line[:4096])

    def _spawn(self, command: list[str], passphrase: str, stdout: int) -> subprocess.Popen:
        self.stop()
        self._stop_event.clear()
        self.last_error = None
        self._diagnostics.clear()
        self._secrets = (passphrase, quote(passphrase, safe=""))
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stdout,
                                       stderr=subprocess.PIPE, bufsize=0, **_popen_options())
        except OSError as exc:
            raise MediaError(f"Could not start FFmpeg: {exc}") from exc
        self._process = process
        self._threads = []
        self._thread(self._read_errors, process, name="ffmpeg-diagnostics")
        self._thread(self._watch, process, name="ffmpeg-exit")
        return process

    def _thread(self, target: Callable, *args, name: str) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _read_errors(self, process: subprocess.Popen) -> None:
        assert process.stderr is not None
        try:
            for line in iter(process.stderr.readline, b""):
                self._log(line.decode("utf-8", errors="replace").rstrip())
        except (OSError, ValueError):
            pass

    def _watch(self, process: subprocess.Popen) -> None:
        code = process.wait()
        for thread in list(self._threads):
            if thread.name == "ffmpeg-diagnostics" and thread is not threading.current_thread():
                thread.join(timeout=1)
        if not self._stop_event.is_set() and self._process is process:
            tail = "; ".join(self.diagnostics[-3:])[-1200:]
            self.last_error = f"FFmpeg ended unexpectedly (status {code})" + (f": {tail}" if tail else "")
            self._log(self.last_error)

    def stop(self) -> None:
        self._stop_event.set()
        process, self._process = self._process, None
        if process is not None:
            if process.poll() is None:
                try:
                    if process.stdin:
                        process.stdin.write(b"q\n")
                        process.stdin.flush()
                    process.wait(timeout=1)
                except (OSError, BrokenPipeError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
            for thread in self._threads:
                if thread is not threading.current_thread():
                    thread.join(timeout=1)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        self._threads = []


class VideoSender(_FFmpegProcess):
    """One captured monitor per encrypted stream; one instance per selected source."""

    def __init__(self, locator: FFmpegLocator | None = None):
        super().__init__(locator)
        self.selected_encoder: str | None = None

    def _encoder(self, config: StreamConfig, synthetic: bool) -> str:
        prefix = "h264" if config.codec == "h264" else "hevc"
        software = "libx264" if config.codec == "h264" else "libx265"
        if config.encoder != "auto":
            if config.encoder not in self.locator.encoders():
                raise MediaError(f"FFmpeg does not include encoder {config.encoder}")
            compatible = config.encoder == software or config.encoder.startswith(prefix + "_")
            if not compatible:
                raise MediaError(f"Encoder {config.encoder} does not match codec {config.codec}")
            if not self.locator.encoder_usable(config.encoder, config.coded_width, config.coded_height, config.fps):
                raise MediaError(f"Encoder {config.encoder} is unavailable; check GPU, driver, and permissions")
            return config.encoder
        candidates = [] if synthetic else ({
            "Windows": [prefix + "_nvenc", prefix + "_qsv", prefix + "_amf"],
            "Darwin": [prefix + "_videotoolbox"],
        }.get(platform.system(), []))
        for candidate in candidates:
            if self.locator.encoder_usable(candidate, config.coded_width, config.coded_height, config.fps):
                return candidate
        if not self.locator.encoder_usable(software):
            raise MediaError(f"No usable {config.codec} encoder. Install an FFmpeg build with {software} or a supported GPU encoder.")
        return software

    def _capture_args(self, source: CaptureSource, config: StreamConfig, synthetic: bool) -> list[str]:
        if synthetic:
            args = ["-re", "-f", "lavfi", "-i", f"testsrc2=size={config.coded_width}x{config.coded_height}:rate={config.fps}"]
            if config.audio:
                args += ["-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
            return args
        if config.audio and not config.audio_device:
            raise MediaError("Select an audio capture device before enabling sound. Windows system sound needs Stereo Mix or a loopback driver; macOS needs a loopback device such as BlackHole.")
        if platform.system() == "Windows":
            if source.index and source.width is None:
                raise MediaError("For a secondary Windows monitor supply its x, y, width and height")
            args = (["-use_wallclock_as_timestamps", "1"] if config.audio else []) + ["-f", "gdigrab", "-framerate", str(config.fps), "-draw_mouse", "1",
                    "-offset_x", str(source.x), "-offset_y", str(source.y)]
            if source.width is not None:
                args += ["-video_size", f"{source.width}x{source.height}"]
            args += ["-i", "desktop"]
            if config.audio:
                args += ["-thread_queue_size", "512", "-use_wallclock_as_timestamps", "1", "-isync", "0",
                         "-f", "dshow", "-audio_buffer_size", "20", "-i", f"audio={config.audio_device}"]
            return args
        if platform.system() == "Darwin":
            screens = self.locator.screen_devices()
            screen = next((screen for index, screen in enumerate(screens)
                           if screen.get("index", index) == source.index), None)
            if screen is None:
                raise MediaError("The requested macOS screen was not found. Grant Screen Recording permission and inspect FFmpeg devices.")
            device = screen["device_index"]
            audio_device = "none"
            if config.audio:
                if not config.audio_device.isdecimal():
                    raise MediaError("Select a numeric macOS AVFoundation audio device index from the audio-device list")
                audio_device = config.audio_device
            return ["-f", "avfoundation", "-framerate", str(config.fps), "-capture_cursor", "1",
                    "-i", f"{device}:{audio_device}"]
        raise MediaError("Native capture is supported on Windows and macOS. Use synthetic=True for a headless Linux test.")

    def start(self, destination: str, port: int, passphrase: str, source: CaptureSource,
              config: StreamConfig, synthetic: bool = False) -> None:
        self.locator.check_ready()
        url = _srt_url(destination, port, passphrase, config.latency_ms, "caller")
        encoder = self._encoder(config, synthetic)
        capture = self._capture_args(source, config, synthetic)
        command = [self.locator.ffmpeg, "-hide_banner", "-loglevel", "warning", "-threads", "2",
                   "-filter_threads", "2"]
        if config.audio:
            command += ["-copyts", "-start_at_zero"]
        command += capture + ["-map", "0:v:0"]
        if config.audio:
            audio_input = 0 if platform.system() == "Darwin" and not synthetic else 1
            command += ["-map", f"{audio_input}:a:0", "-c:a", "aac", "-b:a", "192k",
                        "-ar", "48000", "-ac", "2", "-af", "aresample=async=1000:first_pts=0"]
        else:
            command += ["-an"]
        filters = f"scale={config.width}:{config.height}"
        if config.width != config.coded_width or config.height != config.coded_height:
            filters += f",format=rgb24,pad={config.coded_width}:{config.coded_height}:0:0,format=yuv420p"
        command += ["-vf", filters,
                   "-c:v", encoder, "-threads", "2", "-pix_fmt", "yuv420p", "-bf", "0",
                   "-fps_mode", "passthrough",
                   "-g", str(max(1, config.fps // 2)), "-b:v", f"{config.bitrate_mbps:g}M",
                   "-maxrate", f"{config.bitrate_mbps:g}M", "-bufsize", f"{config.bitrate_mbps:g}M"]
        if encoder == "libx264":
            command += ["-preset", "ultrafast", "-tune", "zerolatency", "-x264-params", "repeat-headers=1:scenecut=0"]
        elif encoder == "libx265":
            command += ["-preset", "ultrafast", "-tune", "zerolatency", "-x265-params", "pools=2:frame-threads=2:repeat-headers=1:log-level=error"]
        elif encoder.endswith("_nvenc"):
            command += ["-preset", "p1", "-tune", "ull", "-zerolatency", "1", "-rc-lookahead", "0"]
        elif encoder.endswith("_qsv"):
            command += ["-preset", "veryfast", "-look_ahead", "0"]
        elif encoder.endswith("_amf"):
            command += ["-usage", "ultralowlatency", "-quality", "speed"]
        elif encoder.endswith("_videotoolbox"):
            command += ["-realtime", "1", "-allow_sw", "0"]
        command += ["-f", "mpegts", "-mpegts_flags", "+resend_headers", "-muxdelay", "0",
                    "-muxpreload", "0", "-flush_packets", "1", url]
        self._spawn(command, passphrase, subprocess.DEVNULL)
        self.selected_encoder = encoder
        if encoder in {"libx264", "libx265"}:
            self._log(f"Using software encoder {encoder}; requested {config.fps} FPS is a target, not a performance guarantee")
        else:
            self._log(f"Using hardware encoder {encoder}")


class VideoReceiver(_FFmpegProcess):
    """SRT listener decoding RGB24 into a bounded, latest-frame callback queue."""

    def __init__(self, locator: FFmpegLocator | None = None):
        super().__init__(locator)
        self.frames_received = 0
        self.frames_dropped = 0
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=1)

    def start(self, bind_address: str, port: int, passphrase: str, config: StreamConfig,
              on_frame: Callable[[bytes, int, int], None]) -> None:
        if not callable(on_frame):
            raise ValueError("on_frame must be callable")
        if config.audio:
            raise MediaError("Use AVReceiver for synchronized sound and video; the RGB receiver is video-only")
        self.locator.check_ready()
        url = _srt_url(bind_address, port, passphrase, config.latency_ms, "listener")
        command = [self.locator.ffmpeg, "-hide_banner", "-loglevel", "warning", "-threads", "2",
                   "-filter_threads", "2", "-probesize", "65536", "-analyzeduration", "100000",
                   "-flags", "low_delay", "-i", url, "-map", "0:v:0", "-an", "-sn", "-dn",
                   "-vf", f"crop={config.width}:{config.height}:0:0:exact=1,scale={config.width}:{config.height}", "-threads", "2",
                   "-pix_fmt", "rgb24", "-fps_mode", "passthrough", "-f", "rawvideo",
                   "-flush_packets", "1", "pipe:1"]
        process = self._spawn(command, passphrase, subprocess.PIPE)
        self.frames_received = self.frames_dropped = 0
        self._frames = queue.Queue(maxsize=1)
        self._thread(self._read_frames, process, config, name="ffmpeg-frames")
        self._thread(self._deliver_frames, process, config, on_frame, name="video-callback")
        deadline = time.monotonic() + 5
        try:
            while not _process_listens_udp(process.pid, port):
                if process.poll() is not None:
                    raise MediaError(f"FFmpeg receiver failed to listen: {'; '.join(self.diagnostics[-5:])}")
                if time.monotonic() >= deadline:
                    raise MediaError("FFmpeg did not bind its SRT listener within five seconds")
                time.sleep(0.03)
        except Exception:
            self.stop()
            raise

    def _read_frames(self, process: subprocess.Popen, config: StreamConfig) -> None:
        assert process.stdout is not None
        frame_size = config.width * config.height * 3
        try:
            # Keep draining stdout during graceful shutdown. Stopping the
            # reader first can fill the pipe, block FFmpeg's writer, and keep
            # it from consuming its quit request.
            while True:
                frame = bytearray()
                while len(frame) < frame_size:
                    chunk = process.stdout.read(frame_size - len(frame))
                    if not chunk:
                        return
                    frame.extend(chunk)
                if self._stop_event.is_set():
                    continue
                self.frames_received += 1
                try:
                    self._frames.put_nowait(bytes(frame))
                except queue.Full:
                    try:
                        self._frames.get_nowait()
                    except queue.Empty:
                        pass
                    self.frames_dropped += 1
                    try:
                        self._frames.put_nowait(bytes(frame))
                    except queue.Full:
                        pass
        except (OSError, ValueError) as exc:
            if not self._stop_event.is_set():
                self._log(f"Video pipe failed: {exc}")

    def _deliver_frames(self, process: subprocess.Popen, config: StreamConfig,
                        on_frame: Callable[[bytes, int, int], None]) -> None:
        while not self._stop_event.is_set():
            try:
                frame = self._frames.get(timeout=0.2)
            except queue.Empty:
                if process.poll() is not None:
                    return
                continue
            try:
                on_frame(frame, config.width, config.height)
            except Exception as exc:
                self._log(f"Video callback failed: {type(exc).__name__}: {exc}")


class AVReceiver(_FFmpegProcess):
    """Remux the timestamped A/V stream to one loopback HTTP player connection.

    The player owns demux, decoding, audio playback, and the shared media clock.
    No independent Python audio/video queues can introduce clock drift. The
    callback runs on a worker thread once the HTTP listener is actually bound;
    a GUI must marshal it to its main thread before touching QMediaPlayer.
    """

    def __init__(self, locator: FFmpegLocator | None = None):
        super().__init__(locator)
        self.url: str | None = None

    def start(self, bind_address: str, port: int, passphrase: str, config: StreamConfig,
              on_ready: Callable[[str], None]) -> None:
        if not callable(on_ready):
            raise ValueError("on_ready must be callable")
        self.locator.check_ready()
        input_url = _srt_url(bind_address, port, passphrase, config.latency_ms, "listener")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            http_port = reservation.getsockname()[1]
        output_url = f"http://127.0.0.1:{http_port}/stream.ts"
        command = [self.locator.ffmpeg, "-hide_banner", "-loglevel", "warning",
                   "-threads", "2", "-probesize", "131072", "-analyzeduration", "200000",
                   "-copyts", "-i", input_url, "-map", "0:v:0"]
        if config.audio:
            command += ["-map", "0:a:0"]
        command += ["-c", "copy", "-f", "mpegts", "-mpegts_copyts", "1",
                    "-muxdelay", "0", "-muxpreload", "0", "-flush_packets", "1",
                    "-listen", "1", "-content_type", "video/MP2T", output_url]
        process = self._spawn(command, passphrase, subprocess.DEVNULL)
        self.url = output_url
        try:
            deadline = time.monotonic() + 5
            while not _process_listens_udp(process.pid, port):
                if process.poll() is not None:
                    raise MediaError(f"FFmpeg A/V receiver failed to listen: {'; '.join(self.diagnostics[-5:])}")
                if time.monotonic() >= deadline:
                    raise MediaError("FFmpeg did not bind its SRT listener within five seconds")
                time.sleep(0.03)
        except Exception:
            self.stop()
            raise
        self._thread(self._notify_http_ready, process, http_port, output_url, on_ready,
                     name="av-http-readiness")

    def _notify_http_ready(self, process: subprocess.Popen, port: int, url: str,
                           on_ready: Callable[[str], None]) -> None:
        deadline = time.monotonic() + 30
        try:
            while not self._stop_event.is_set() and process.poll() is None:
                if _process_owns_listener(process.pid, port, tcp=True):
                    if not self._stop_event.is_set():
                        on_ready(url)
                    return
                if time.monotonic() >= deadline:
                    raise MediaError("Timed out waiting for video/audio streams. Check capture devices and the sender's diagnostics.")
                self._stop_event.wait(0.05)
        except Exception as exc:
            if not self._stop_event.is_set():
                self.last_error = f"A/V relay failed: {exc}"
                self._log(self.last_error)
                self.stop()

    def stop(self) -> None:
        super().stop()
        self.url = None
