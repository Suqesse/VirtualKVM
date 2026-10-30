"""Bounded, pinned-TLS control, clipboard and file transfer protocol."""
from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import re
import secrets
import select
import socket
import ssl
import struct
import tempfile
import threading
import time
from typing import Callable

from .security import SecurityError, connect_pinned, pairing_proof, server_identity
from .shared import SharedDirectory, SharedRead, SharedUpload
from .transport import TLSConnection

MAX_MESSAGE = 1024 * 1024
MAX_FILE_SIZE = 1024 * 1024 * 1024
MAX_CLIPBOARD_BYTES = 256 * 1024
CHUNK_SIZE = 64 * 1024


class ProtocolError(RuntimeError):
    """The peer sent an invalid message or refused a request."""


def _send_message(connection: ssl.SSLSocket, message: dict, lock: threading.Lock) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not payload or len(payload) > MAX_MESSAGE:
        raise ProtocolError("Control message exceeds the size limit")
    with lock:
        connection.sendall(struct.pack("!I", len(payload)) + payload)


def _read_exact(connection: ssl.SSLSocket, count: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Control frame deadline exceeded")
        connection.settimeout(remaining)
        block = connection.recv(count - len(data))
        if not block:
            raise EOFError("Peer disconnected")
        data.extend(block)
    return bytes(data)


def _receive_message(connection: ssl.SSLSocket, timeout: float = 10) -> dict:
    previous_timeout = connection.gettimeout()
    deadline = time.monotonic() + min(timeout, previous_timeout if previous_timeout is not None else timeout)
    try:
        size, = struct.unpack("!I", _read_exact(connection, 4, deadline))
        if not 0 < size <= MAX_MESSAGE:
            raise ProtocolError("Invalid control message size")
        try:
            message = json.loads(_read_exact(connection, size, deadline), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ProtocolError("Invalid JSON control message") from error
    finally:
        try:
            connection.settimeout(previous_timeout)
        except OSError:
            pass
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise ProtocolError("Control messages must be typed objects")
    identifier = message.get("id")
    if identifier is not None and (not isinstance(identifier, str) or not 1 <= len(identifier) <= 128):
        raise ProtocolError("Invalid request identifier")
    return message


def _clipboard(text: object) -> str:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_CLIPBOARD_BYTES:
        raise ProtocolError("Clipboard text exceeds the 256 KiB limit")
    return text


def _filename(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 240:
        raise ProtocolError("Invalid filename")
    if value in (".", "..") or value.endswith((".", " ")) or re.search(r'[\\/:\x00-\x1f<>"|?*]', value):
        raise ProtocolError("Invalid filename")
    if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ProtocolError("Reserved filename")
    return value


def _notify(callback: Callable | None, *args) -> None:
    if callback:
        try:
            callback(*args)
        except Exception:
            # UI callbacks must not prevent socket/resource cleanup.
            pass


class _Transfer:
    def __init__(self, directory: Path, name: str, size: int):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.destination = directory / name
        if self.destination.exists():
            raise ProtocolError("Destination file already exists")
        descriptor, temporary = tempfile.mkstemp(prefix=".virtualkvm-", suffix=".part", dir=directory)
        self.path = Path(temporary)
        self.stream = os.fdopen(descriptor, "wb")
        self.expected_size = size
        self.received = 0
        self.digest = hashlib.sha256()

    def append(self, encoded: object) -> None:
        if not isinstance(encoded, str) or len(encoded) > 4 * ((CHUNK_SIZE + 2) // 3):
            raise ProtocolError("Invalid file chunk size")
        try:
            block = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ProtocolError("Invalid file chunk encoding") from error
        if not block or len(block) > CHUNK_SIZE or self.received + len(block) > self.expected_size:
            raise ProtocolError("File size exceeds its declared limit")
        self.stream.write(block)
        self.digest.update(block)
        self.received += len(block)

    def finish(self, checksum: object) -> dict:
        actual = self.digest.hexdigest()
        if not isinstance(checksum, str) or self.received != self.expected_size or not hmac.compare_digest(checksum, actual):
            raise ProtocolError("Transferred file size or SHA-256 mismatch")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        try:
            # Atomic installation without replacing an existing destination.
            os.link(self.path, self.destination)
        except OSError as error:
            raise ProtocolError("Cannot install received file without overwriting an existing file") from error
        self.path.unlink()
        return {"path": str(self.destination), "size": self.received, "sha256": actual}

    def close(self):
        try:
            self.stream.close()
        except OSError:
            pass
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


class _Session:
    def __init__(self, connection: ssl.SSLSocket, peer: tuple):
        self.connection = connection
        self.peer = peer
        self.lock = threading.Lock()
        self.transfer: _Transfer | SharedRead | SharedUpload | None = None

    def send(self, message: dict):
        _send_message(self.connection, message, self.lock)


class HostServer:
    def __init__(self, bind="0.0.0.0", port=24800, *, state_dir: Path, pin: str,
                 on_event: Callable[[dict], None], on_clipboard=None, receive_dir=None,
                 on_status=None, on_disconnect=None, displays=None, on_stream_request=None,
                 on_stream_start=None, on_stream_stop=None, shared_dir: Path | None = None,
                 shared_writable: bool = False):
        # Validate the PIN without retaining any derived proof.
        pairing_proof(pin, "00" * 32, "00" * 32)
        self.bind, self.port = bind, port
        self.state_dir = Path(state_dir)
        self.receive_dir = Path(receive_dir) if receive_dir is not None else self.state_dir / "received"
        self.shared = SharedDirectory(shared_dir, shared_writable, forbidden=self.state_dir)
        self.pin = pin
        self.on_event, self.on_clipboard = on_event, on_clipboard
        self.on_status, self.on_disconnect = on_status, on_disconnect
        self.on_stream_request, self.on_stream_start, self.on_stream_stop = on_stream_request, on_stream_start, on_stream_stop
        self.displays = displays or []
        self._context, self.fingerprint = server_identity(self.state_dir)
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._threads: set[threading.Thread] = set()
        self._connections: set[socket.socket | TLSConnection] = set()
        self._session: _Session | None = None
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._attempts: dict[str, collections.deque] = {}

    @property
    def address(self):
        return self._listener.getsockname()[:2] if self._listener else (self.bind, self.port)

    @property
    def sessions(self):
        with self._lock:
            return [{"host": self._session.peer[0], "port": self._session.peer[1]}] if self._session else []

    def start(self):
        if self._listener:
            raise RuntimeError("Host is already running")
        self._stopping.clear()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((self.bind, self.port))
            listener.listen(8)
            listener.settimeout(0.2)
        except BaseException:
            listener.close()
            raise
        self._listener = listener
        self._accept_thread = threading.Thread(target=self._accept, args=(listener,), name="virtualkvm-host", daemon=True)
        self._accept_thread.start()
        _notify(self.on_status, "Host listening")

    def stop(self):
        self._stopping.set()
        listener, self._listener = self._listener, None
        if listener:
            _shutdown(listener)
        with self._lock:
            connections = list(self._connections)
        for connection in connections:
            _shutdown(connection)
        current = threading.current_thread()
        if self._accept_thread and self._accept_thread is not current:
            self._accept_thread.join(timeout=2)
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            if thread is not current:
                thread.join(timeout=3)
        _notify(self.on_status, "Host stopped")

    def _accept(self, listener):
        while not self._stopping.is_set():
            try:
                connection, peer = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                if len(self._connections) >= 8:
                    connection.close()
                    continue
                self._connections.add(connection)
                thread = threading.Thread(target=self._serve, args=(connection, peer), name="virtualkvm-peer", daemon=True)
                self._threads.add(thread)
                thread.start()

    def _handshake(self, connection: ssl.SSLSocket) -> bool:
        # Closing a socket from another thread does not reliably wake macOS's
        # SSL readiness wait. Poll a nonblocking handshake so shutdown has its
        # own bound instead of waiting for each peer's five-second timeout.
        connection.setblocking(False)
        deadline = time.monotonic() + 5
        while not self._stopping.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("TLS handshake exceeded its deadline")
            try:
                connection.do_handshake()
                connection.settimeout(5)
                return True
            except ssl.SSLWantReadError:
                readable, writable = [connection], []
            except ssl.SSLWantWriteError:
                readable, writable = [], [connection]
            except AttributeError:
                # SSLSocket.shutdown clears _sslobj while stop closes peers.
                if self._stopping.is_set():
                    return False
                raise
            try:
                select.select(readable, writable, [], min(0.1, remaining))
            except (OSError, ValueError):
                if self._stopping.is_set():
                    return False
                raise OSError("TLS handshake socket became unavailable") from None
        return False

    def _serve(self, raw: socket.socket, peer):
        connection = raw
        session = None
        try:
            raw.settimeout(5)
            with self._lock:
                connection = self._context.wrap_socket(raw, server_side=True, do_handshake_on_connect=False)
                self._connections.discard(raw)
                self._connections.add(connection)
                if self._stopping.is_set():
                    return
            if not self._handshake(connection):
                return
            secured = connection
            connection = TLSConnection(secured)
            with self._lock:
                self._connections.discard(secured)
                self._connections.add(connection)
                if self._stopping.is_set():
                    return
            connection.settimeout(5)
            session = _Session(connection, peer)
            now = time.monotonic()
            with self._lock:
                attempts = self._attempts.setdefault(peer[0], collections.deque())
                while attempts and now - attempts[0] > 300:
                    attempts.popleft()
                if len(attempts) >= 5:
                    raise SecurityError("Too many pairing attempts; wait five minutes")
            nonce = secrets.token_hex(32)
            session.send({"type": "challenge", "version": 1, "nonce": nonce})
            authentication = _receive_message(connection)
            expected = pairing_proof(self.pin, nonce, self.fingerprint)
            proof = authentication.get("proof")
            if authentication.get("type") != "authenticate" or not isinstance(proof, str) or not hmac.compare_digest(proof, expected):
                with self._lock:
                    attempts.append(time.monotonic())
                session.send({"type": "error", "error": "Pairing failed"})
                raise SecurityError("Pairing failed")
            with self._lock:
                if self._session is not None:
                    session.send({"type": "error", "error": "Another viewer already controls this host"})
                    return
                self._session = session
            session.send({"type": "authenticated", "version": 1, "displays": self.displays,
                          "shared": self.shared.info})
            # An unresponsive peer must release remote keys within a bounded
            # interval, including cable loss where TCP has not reported EOF.
            connection.settimeout(10)
            _notify(self.on_status, "Viewer connected")
            while not self._stopping.is_set():
                message = _receive_message(connection)
                try:
                    result = self._handle(session, message)
                    if "id" in message:
                        session.send({"type": "response", "id": message["id"], "result": result})
                except Exception as error:
                    transfer_type = {"file_chunk": _Transfer, "file_end": _Transfer,
                                     "shared_read": SharedRead,
                                     "shared_write_chunk": SharedUpload, "shared_write_end": SharedUpload}.get(message["type"])
                    # Reject a second transfer without aborting the first one;
                    # input/clipboard callback errors must not corrupt a copy.
                    if transfer_type and isinstance(session.transfer, transfer_type):
                        session.transfer.close()
                        session.transfer = None
                    session.send({"type": "error", "id": message.get("id"), "error": str(error)[:300]})
        except (OSError, EOFError, ProtocolError, SecurityError):
            pass
        finally:
            if session and session.transfer:
                session.transfer.close()
            with self._lock:
                was_controller = self._session is session and session is not None
                if was_controller:
                    self._session = None
                self._connections.discard(raw)
                self._connections.discard(connection)
            _shutdown(connection)
            if was_controller:
                _notify(self.on_disconnect)
                _notify(self.on_status, "Viewer disconnected")
            with self._lock:
                self._threads.discard(threading.current_thread())

    def _handle(self, session: _Session, message: dict):
        kind = message["type"]
        if kind == "event":
            event = message.get("event")
            if not isinstance(event, dict):
                raise ProtocolError("Invalid input event")
            self.on_event(event)
        elif kind == "clipboard":
            text = _clipboard(message.get("text"))
            if self.on_clipboard:
                self.on_clipboard(text)
        elif kind == "stream_request":
            displays, config = message.get("displays"), message.get("config")
            if not isinstance(displays, list) or not 1 <= len(displays) <= 8 or any(type(i) is not int or i < 0 for i in displays) or len(set(displays)) != len(displays):
                raise ProtocolError("Choose one to eight distinct display indexes")
            if not isinstance(config, dict):
                raise ProtocolError("Invalid stream configuration")
            if not self.on_stream_request:
                raise ProtocolError("Video streaming is not configured")
            request = {"type": kind, "displays": displays, "config": config, "_peer_host": session.peer[0]}
            return self.on_stream_request(request)
        elif kind == "stream_start":
            if not self.on_stream_start:
                raise ProtocolError("Video streaming is not configured")
            return self.on_stream_start()
        elif kind == "stream_stop":
            if self.on_stream_stop:
                return self.on_stream_stop()
        elif kind == "file_begin":
            if session.transfer:
                raise ProtocolError("A file transfer is already in progress")
            if (self.shared.root is not None and not self.shared.writable
                    and self.receive_dir.resolve().is_relative_to(self.shared.root)):
                raise ProtocolError("The receive folder is inside the read-only shared folder")
            size = message.get("size")
            if type(size) is not int or not 0 <= size <= MAX_FILE_SIZE:
                raise ProtocolError("File exceeds the 1 GiB transfer limit")
            session.transfer = _Transfer(self.receive_dir, _filename(message.get("name")), size)
        elif kind == "file_chunk":
            if not isinstance(session.transfer, _Transfer):
                raise ProtocolError("No file transfer is in progress")
            session.transfer.append(message.get("data"))
        elif kind == "file_end":
            if not isinstance(session.transfer, _Transfer):
                raise ProtocolError("No file transfer is in progress")
            transfer, session.transfer = session.transfer, None
            try:
                result = transfer.finish(message.get("sha256"))
                _notify(self.on_status, f"Received file: {transfer.destination.name}")
                return result
            finally:
                transfer.close()
        elif kind == "file_cancel":
            if session.transfer:
                session.transfer.close()
                session.transfer = None
        elif kind == "shared_list":
            return self.shared.list(message.get("path", ""))
        elif kind == "shared_mkdir":
            return self.shared.mkdir(message.get("path"))
        elif kind == "shared_read_begin":
            if session.transfer:
                raise ProtocolError("A file transfer is already in progress")
            session.transfer = self.shared.read(message.get("path"))
            return {"size": session.transfer.size, "path": session.transfer.path}
        elif kind == "shared_read":
            if not isinstance(session.transfer, SharedRead):
                raise ProtocolError("No shared download is in progress")
            block, result = session.transfer.read(message.get("offset"), message.get("length"))
            result["data"] = base64.b64encode(block).decode("ascii")
            if result["eof"]:
                session.transfer.close()
                session.transfer = None
            return result
        elif kind == "shared_write_begin":
            if session.transfer:
                raise ProtocolError("A file transfer is already in progress")
            session.transfer = self.shared.upload(message.get("path"), message.get("size"))
        elif kind == "shared_write_chunk":
            if not isinstance(session.transfer, SharedUpload):
                raise ProtocolError("No shared upload is in progress")
            encoded = message.get("data")
            if not isinstance(encoded, str) or len(encoded) > 4 * ((CHUNK_SIZE + 2) // 3):
                raise ProtocolError("Invalid shared file chunk")
            try:
                block = base64.b64decode(encoded, validate=True)
            except ValueError as error:
                raise ProtocolError("Invalid shared file chunk") from error
            session.transfer.append(block)
        elif kind == "shared_write_end":
            if not isinstance(session.transfer, SharedUpload):
                raise ProtocolError("No shared upload is in progress")
            transfer, session.transfer = session.transfer, None
            try:
                return transfer.finish(message.get("sha256"))
            finally:
                transfer.close()
        elif kind in ("shared_read_cancel", "shared_write_cancel"):
            if isinstance(session.transfer, (SharedRead, SharedUpload)):
                session.transfer.close()
                session.transfer = None
        elif kind == "ping":
            return {"time": time.monotonic()}
        else:
            raise ProtocolError("Unknown control request")
        return {}

    def broadcast_clipboard(self, text: str):
        text = _clipboard(text)
        with self._lock:
            session = self._session
        if session:
            session.send({"type": "clipboard", "text": text})

    def broadcast_status(self, text: str):
        if not isinstance(text, str) or len(text.encode("utf-8")) > 4096:
            raise ProtocolError("Status text exceeds the 4 KiB limit")
        with self._lock:
            session = self._session
        if session:
            session.send({"type": "status", "text": text})


def _shutdown(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()


class ViewerClient:
    def __init__(self, state_dir: Path, on_clipboard=None, on_status=None):
        self.state_dir = Path(state_dir)
        self.on_clipboard, self.on_status = on_clipboard, on_status
        self.displays = []
        self.shared_info = {"enabled": False, "writable": False, "name": ""}
        self._transfer_lock = threading.Lock()
        self._connection: ssl.SSLSocket | None = None
        self._send_lock = threading.Lock()
        self._lock = threading.RLock()
        self._pending: dict[str, queue.Queue] = {}
        self._reader: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()

    @property
    def connected(self):
        return self._connection is not None

    def connect(self, host, port=24800, pin=None, fingerprint=None):
        if self._connection:
            raise RuntimeError("Viewer is already connected")
        connection = TLSConnection(connect_pinned(host, port, fingerprint))
        connection.settimeout(5)
        try:
            challenge = _receive_message(connection)
            if challenge.get("type") != "challenge" or challenge.get("version") != 1:
                raise ProtocolError(challenge.get("error", "Unsupported host protocol"))
            proof = pairing_proof(pin, challenge.get("nonce"), fingerprint)
            _send_message(connection, {"type": "authenticate", "proof": proof}, self._send_lock)
            response = _receive_message(connection)
            if response.get("type") != "authenticated" or response.get("version") != 1:
                raise SecurityError(response.get("error", "Pairing failed"))
            self.displays = response.get("displays", [])
            self.shared_info = response.get("shared", {"enabled": False, "writable": False, "name": ""})
            connection.settimeout(10)
            self._connection = connection
            self._heartbeat_stop = threading.Event()
            self._reader = threading.Thread(target=self._read, args=(connection,), name="virtualkvm-viewer", daemon=True)
            self._reader.start()
            self._heartbeat = threading.Thread(target=self._heartbeat_loop,
                args=(connection, self._heartbeat_stop), name="virtualkvm-heartbeat", daemon=True)
            self._heartbeat.start()
            _notify(self.on_status, "Connected")
            return response
        except BaseException:
            _shutdown(connection)
            raise

    def disconnect(self):
        self._heartbeat_stop.set()
        with self._lock:
            connection, self._connection = self._connection, None
        if connection:
            _shutdown(connection)
        reader = self._reader
        if reader and reader is not threading.current_thread():
            reader.join(timeout=3)
        self._fail_pending("Viewer disconnected")
        heartbeat = self._heartbeat
        if heartbeat and heartbeat is not threading.current_thread():
            heartbeat.join(timeout=2)

    def _heartbeat_loop(self, connection, stopped):
        while not stopped.wait(2):
            if self._connection is not connection:
                return
            try:
                self._request("ping", timeout=5)
            except (OSError, ProtocolError):
                _shutdown(connection)
                return

    def _fail_pending(self, reason):
        with self._lock:
            pending, self._pending = self._pending, {}
        for receiver in pending.values():
            receiver.put({"type": "error", "error": reason})

    def _read(self, connection):
        try:
            while True:
                message = _receive_message(connection)
                identifier = message.get("id")
                if identifier:
                    with self._lock:
                        receiver = self._pending.get(identifier)
                    if receiver:
                        receiver.put(message)
                elif message["type"] == "clipboard":
                    _notify(self.on_clipboard, _clipboard(message.get("text")))
                elif message["type"] == "error":
                    _notify(self.on_status, message.get("error", "Host refused an event"))
                elif message["type"] == "status":
                    text = message.get("text")
                    if not isinstance(text, str) or len(text.encode("utf-8")) > 4096:
                        raise ProtocolError("Invalid host status message")
                    _notify(self.on_status, text)
        except (OSError, EOFError, ProtocolError) as error:
            if not self._heartbeat_stop.is_set():
                _notify(self.on_status, f"Control connection failed: {type(error).__name__}: {error}")
        finally:
            self._heartbeat_stop.set()
            with self._lock:
                if self._connection is connection:
                    self._connection = None
            _shutdown(connection)
            self._fail_pending("Host disconnected")
            _notify(self.on_status, "Disconnected")

    def _send(self, message):
        connection = self._connection
        if not connection:
            raise ProtocolError("Viewer is not connected")
        _send_message(connection, message, self._send_lock)

    def _request(self, kind, timeout=15, **fields):
        identifier = secrets.token_hex(12)
        receiver = queue.Queue()
        with self._lock:
            self._pending[identifier] = receiver
        try:
            self._send({"type": kind, "id": identifier, **fields})
            try:
                response = receiver.get(timeout=timeout)
            except queue.Empty as error:
                raise ProtocolError("Host request timed out") from error
            if response.get("type") == "error":
                raise ProtocolError(response.get("error", "Host refused the request"))
            return response.get("result", {})
        finally:
            with self._lock:
                self._pending.pop(identifier, None)

    def send_event(self, event):
        if not isinstance(event, dict):
            raise ProtocolError("Invalid input event")
        self._send({"type": "event", "event": event})

    def send_clipboard(self, text):
        self._send({"type": "clipboard", "text": _clipboard(text)})

    def request_stream(self, displays, config):
        return self._request("stream_request", displays=displays, config=config)

    def start_streams(self):
        return self._request("stream_start")

    def stop_streams(self):
        return self._request("stream_stop")

    def send_file(self, path: Path):
        with self._transfer_lock:
            return self._send_file(path)

    def _send_file(self, path: Path):
        path = Path(path)
        name = _filename(path.name)
        size = path.stat().st_size
        if size > MAX_FILE_SIZE or not path.is_file():
            raise ProtocolError("Select a regular file of at most 1 GiB")
        self._request("file_begin", name=name, size=size)
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                while block := stream.read(CHUNK_SIZE):
                    digest.update(block)
                    self._request("file_chunk", data=base64.b64encode(block).decode("ascii"))
            return self._request("file_end", sha256=digest.hexdigest())
        except BaseException:
            try:
                self._request("file_cancel", timeout=2)
            except Exception:
                pass
            raise

    def shared_list(self, path=""):
        return self._request("shared_list", path=path)

    def shared_mkdir(self, path):
        return self._request("shared_mkdir", path=path)

    def shared_upload(self, local_path: Path, remote_path: str):
        with self._transfer_lock:
            source = Path(local_path)
            size = source.stat().st_size
            if not source.is_file() or size > MAX_FILE_SIZE:
                raise ProtocolError("Select a regular file of at most 1 GiB")
            self._request("shared_write_begin", path=remote_path, size=size)
            digest = hashlib.sha256()
            try:
                with source.open("rb") as stream:
                    while block := stream.read(CHUNK_SIZE):
                        digest.update(block)
                        self._request("shared_write_chunk", data=base64.b64encode(block).decode("ascii"))
                return self._request("shared_write_end", sha256=digest.hexdigest())
            except BaseException:
                try:
                    self._request("shared_write_cancel", timeout=2)
                except Exception:
                    pass
                raise

    def shared_download(self, remote_path: str, local_path: Path):
        with self._transfer_lock:
            destination = Path(local_path)
            if destination.exists() or destination.is_symlink():
                raise ProtocolError("The local destination already exists")
            if not destination.parent.is_dir():
                raise ProtocolError("Choose an existing local destination folder")
            metadata = self._request("shared_read_begin", path=remote_path)
            temporary = None
            try:
                size = metadata.get("size")
                if type(size) is not int or not 0 <= size <= MAX_FILE_SIZE:
                    raise ProtocolError("Invalid shared file size")
                descriptor, temporary = tempfile.mkstemp(prefix=".virtualkvm-", suffix=".part", dir=destination.parent)
                offset, digest = 0, hashlib.sha256()
                with os.fdopen(descriptor, "wb") as stream:
                    while True:
                        result = self._request("shared_read", offset=offset, length=CHUNK_SIZE)
                        encoded = result.get("data")
                        if not isinstance(encoded, str) or len(encoded) > 4 * ((CHUNK_SIZE + 2) // 3):
                            raise ProtocolError("Invalid shared download chunk")
                        block = base64.b64decode(encoded, validate=True)
                        if result.get("offset") != offset or len(block) > CHUNK_SIZE or offset + len(block) > size:
                            raise ProtocolError("Invalid shared download range")
                        stream.write(block)
                        digest.update(block)
                        offset += len(block)
                        if result.get("eof"):
                            checksum = result.get("sha256")
                            if offset != size or not isinstance(checksum, str) or not hmac.compare_digest(checksum, digest.hexdigest()):
                                raise ProtocolError("Shared download size or SHA-256 mismatch")
                            break
                        if not block:
                            raise ProtocolError("Shared download ended unexpectedly")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, destination)
                Path(temporary).unlink()
                temporary = None
                return {"path": str(destination), "size": offset, "sha256": digest.hexdigest()}
            except BaseException:
                try:
                    self._request("shared_read_cancel", timeout=2)
                except Exception:
                    pass
                raise
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
