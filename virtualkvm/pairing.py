"""Explicit local session approval and a short code bound to the TLS session."""
from dataclasses import asdict, dataclass
import hashlib
import threading
import time

from .security import SecurityError, normalize_fingerprint


@dataclass(frozen=True)
class Permissions:
    control: bool = True
    clipboard: bool = False
    files: bool = False
    audio: bool = False

    def encode(self):
        return asdict(self)

    @classmethod
    def parse(cls, value):
        if not isinstance(value, dict) or set(value) != {'control', 'clipboard', 'files', 'audio'}:
            raise SecurityError('Invalid session permissions')
        if any(type(item) is not bool for item in value.values()):
            raise SecurityError('Invalid session permission type')
        return cls(**value)


LEGACY_PERMISSIONS = Permissions(True, True, True, True)


def session_code(connection, nonce, fingerprint):
    binding = connection.channel_binding()
    if not binding or len(binding) < 12:
        raise SecurityError('This TLS backend cannot verify quick pairing')
    transcript = b'VirtualKVM local approval v1\0' + binding + bytes.fromhex(nonce) + bytes.fromhex(normalize_fingerprint(fingerprint))
    code = hashlib.sha256(transcript).hexdigest()[:12].upper()
    return ' '.join(code[index:index + 4] for index in range(0, 12, 4))


class AccessRequest:
    def __init__(self, name, code, control=True):
        if not isinstance(name, str) or not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name):
            raise SecurityError('Invalid computer name')
        if type(control) is not bool:
            raise SecurityError('Invalid control request')
        self.name, self.code, self.control = name, code, control
        self.expires = time.monotonic() + 60
        self.done = threading.Event()
        self._lock = threading.Lock()
        self._grant = None

    def resolve(self, grant=None):
        if grant is not None and not isinstance(grant, Permissions):
            raise TypeError('Expected explicit session permissions')
        with self._lock:
            if not self.done.is_set():
                self._grant = grant
                self.done.set()

    def wait(self, stopped, alive=None):
        while time.monotonic() < self.expires and not stopped.is_set():
            if self.done.wait(0.1):
                return self._grant
            if alive is not None and not alive():
                break
        self.resolve()
        return None
