"""LAN discovery via standard Bonjour/mDNS, with no PINs in advertisements."""
from __future__ import annotations

from dataclasses import dataclass
import json
import ipaddress
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import uuid

from .security import normalize_fingerprint

SERVICE = '_virtualkvm._tcp.local.'


@dataclass(frozen=True)
class Device:
    identifier: str
    name: str
    address: str
    port: int
    fingerprint: str
    quick_pairing: bool

    def __post_init__(self):
        object.__setattr__(self, 'fingerprint', normalize_fingerprint(self.fingerprint))


def decode_device(info, networks=()):
    try:
        properties = {key.decode('ascii'): value.decode('utf-8') for key, value in info.properties.items()
                      if isinstance(key, bytes) and isinstance(value, bytes)}
        identifier, name = properties['id'], properties['name']
        if not re.fullmatch(r'[0-9a-f]{32}', identifier):
            return None
        if not 1 <= len(name) <= 80 or any(ord(c) < 32 for c in name):
            return None
        addresses = [value for value in info.parsed_addresses() if ':' not in value]
        if not addresses or type(info.port) is not int or not 1 <= info.port <= 65535:
            return None
        address = addresses[0]
        address = next((candidate for network in networks for candidate in addresses
                        if ipaddress.ip_address(candidate) in network), address)
        # Accept only real IPv4 endpoints, never arbitrary hostnames/URLs from TXT.
        socket.inet_pton(socket.AF_INET, address)
        fingerprint = normalize_fingerprint(properties['fingerprint'])
        return Device(identifier, name, address, info.port, fingerprint, properties.get('quick') == '1')
    except (ValueError, KeyError, TypeError, UnicodeError, OSError, AttributeError):
        return None


def device_identifier(state_dir):
    path = Path(state_dir) / 'device-id'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        value = path.read_text().strip()
        if not re.fullmatch(r'[0-9a-f]{32}', value):
            raise ValueError('Invalid saved device identifier')
        return value
    value = uuid.uuid4().hex
    with os.fdopen(descriptor, 'w') as target:
        target.write(value)
    return value


class TrustStore:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / 'trusted-computers.json'

    def _read(self):
        try:
            if self.path.stat().st_size > 65536:
                return {}
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def state(self, device):
        saved = self._read().get(device.identifier)
        return 'new' if saved is None else 'known' if saved == device.fingerprint else 'changed'

    def remember(self, device):
        # Store the verified host identity only, never pairing secrets or PINs.
        data = self._read()
        if len(data) >= 256 and device.identifier not in data:
            data.pop(next(iter(data)))
        data[device.identifier] = normalize_fingerprint(device.fingerprint)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile('w', dir=self.path.parent, prefix='trust-', delete=False) as target:
            temporary = Path(target.name)
            json.dump(data, target)
        try:
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


class DiscoveryService:
    def __init__(self, identifier, name, addresses, port, fingerprint, on_devices, *, interfaces=None, service_type=SERVICE):
        self.identifier, self.name = identifier, name[:80]
        self.addresses, self.port, self.fingerprint = addresses, port, fingerprint
        self.on_devices, self.interfaces = on_devices, interfaces
        self.service_type = service_type
        self._zeroconf = self._browser = self._info = None
        self._devices = {}
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._networks = []

    def start(self):
        import ifaddr
        from zeroconf import IPVersion, InterfaceChoice, ServiceBrowser, ServiceInfo, Zeroconf
        self._networks = [ipaddress.ip_network(f'{entry.ip}/{entry.network_prefix}', strict=False)
                          for adapter in ifaddr.get_adapters() for entry in adapter.ips
                          if isinstance(entry.ip, str) and entry.ip in self.addresses]
        zeroconf = Zeroconf(interfaces=self.interfaces or InterfaceChoice.All, ip_version=IPVersion.V4Only)
        self._zeroconf = zeroconf
        try:
            self._info = ServiceInfo(self.service_type, f'VirtualKVM-{self.identifier}.{self.service_type}',
                addresses=[socket.inet_aton(address) for address in self.addresses], port=self.port,
                properties={'id': self.identifier, 'name': self.name, 'fingerprint': normalize_fingerprint(self.fingerprint),
                            'quick': '1'}, server=f'virtualkvm-{self.identifier}.local.', host_ttl=30, other_ttl=30)
            zeroconf.register_service(self._info)
            self._browser = ServiceBrowser(zeroconf, self.service_type, listener=self)
        except BaseException:
            zeroconf.close()
            self._zeroconf = None
            raise

    def add_service(self, zeroconf, kind, name):
        self.update_service(zeroconf, kind, name)

    def update_service(self, zeroconf, kind, name):
        if self._stopped.is_set():
            return
        info = zeroconf.get_service_info(kind, name, timeout=1500)
        device = decode_device(info, self._networks)
        if device is None:
            self.remove_service(zeroconf, kind, name)
            return
        if device.identifier == self.identifier:
            return
        with self._lock:
            self._devices[name] = device
            devices = sorted(self._devices.values(), key=lambda item: item.name.casefold())
        self.on_devices(devices)

    def remove_service(self, zeroconf, kind, name):
        with self._lock:
            self._devices.pop(name, None)
            devices = sorted(self._devices.values(), key=lambda item: item.name.casefold())
        if not self._stopped.is_set():
            self.on_devices(devices)

    def refresh(self):
        # Browsing is continuous; a fresh browser also queries current peers.
        if self._zeroconf is not None and not self._stopped.is_set():
            from zeroconf import ServiceBrowser
            if self._browser:
                self._browser.cancel()
            self._browser = ServiceBrowser(self._zeroconf, self.service_type, listener=self)

    def set_port(self, port):
        if self._zeroconf is not None and self._info is not None and not self._stopped.is_set():
            self._info.port = port
            self._zeroconf.update_service(self._info)

    def stop(self):
        self._stopped.set()
        if self._browser:
            self._browser.cancel()
        if self._zeroconf:
            self._zeroconf.close()
        self._zeroconf = self._browser = None
