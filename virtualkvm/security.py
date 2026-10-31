"""Pinned TLS identities for VirtualKVM's control plane.

The fingerprint must be compared through the host's local display before pairing.
No certificate authorities or first-connection trust bypass are used.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


class SecurityError(ValueError):
    """A peer identity or pairing proof could not be verified."""


def normalize_fingerprint(value: str) -> str:
    if not isinstance(value, str):
        raise SecurityError("The host SHA-256 fingerprint is required")
    normalized = value.replace(":", "").replace(" ", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise SecurityError("Enter the complete 64-digit host SHA-256 fingerprint")
    return normalized


def certificate_fingerprint(der: bytes) -> str:
    raw = hashlib.sha256(der).hexdigest().upper()
    return ":".join(raw[i:i + 2] for i in range(0, len(raw), 2))


def generate_pin() -> str:
    """A 12-digit local pairing code; never write this value to logs."""
    return "".join(secrets.choice("0123456789") for _ in range(12))


def pairing_proof(pin: str, nonce: str, fingerprint: str) -> str:
    if not isinstance(pin, str) or not 6 <= len(pin) <= 128:
        raise SecurityError("Pairing PIN must contain between 6 and 128 characters")
    try:
        challenge = bytes.fromhex(nonce)
    except (TypeError, ValueError) as error:
        raise SecurityError("Invalid pairing challenge") from error
    if len(challenge) != 32:
        raise SecurityError("Invalid pairing challenge")
    transcript = b"VirtualKVM control v1\0" + challenge + bytes.fromhex(normalize_fingerprint(fingerprint))
    return hmac.new(pin.encode("utf-8"), transcript, hashlib.sha256).hexdigest()


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def server_identity(state_dir: Path) -> tuple[ssl.SSLContext, str]:
    """Load or create a persistent self-signed EC identity in a private directory."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    certificate_path = state_dir / "host-cert.pem"
    key_path = state_dir / "host-key.pem"
    if certificate_path.exists() != key_path.exists():
        raise SecurityError("Incomplete host identity: preserve and repair the certificate/key pair")
    if not certificate_path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "VirtualKVM Host")])
        now = datetime.now(timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                       .public_key(key.public_key()).serial_number(x509.random_serial_number())
                       .not_valid_before(now - timedelta(minutes=5))
                       .not_valid_after(now + timedelta(days=3650))
                       .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                       .sign(key, hashes.SHA256()))
        _write_private(key_path, key.private_bytes(serialization.Encoding.PEM,
                       serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        try:
            _write_private(certificate_path, certificate.public_bytes(serialization.Encoding.PEM))
        except BaseException:
            key_path.unlink(missing_ok=True)
            raise
    certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
    fingerprint = certificate_fingerprint(certificate.public_bytes(serialization.Encoding.DER))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(certificate_path), str(key_path))
    return context, fingerprint


def connect_pinned(host: str, port: int, fingerprint: str, timeout: float = 5) -> ssl.SSLSocket:
    """Connect using explicit certificate pinning, independent of CA trust."""
    import socket

    expected = normalize_fingerprint(fingerprint)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    # A self-signed certificate is authenticated by its full DER SHA-256 pin.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        secured = context.wrap_socket(raw, server_hostname=host)
        actual = hashlib.sha256(secured.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(expected, actual):
            secured.close()
            raise SecurityError("Host fingerprint mismatch; pairing was refused")
        return secured
    except BaseException:
        raw.close()
        raise


def probe_fingerprint(host: str, port: int, timeout: float = 5) -> str:
    """Read public identity only. Never send credentials or authorize this key.

    The subsequent connection still pins this exact certificate and requires
    owner approval with a TLS-bound short code. This probe is not a trust bypass.
    """
    import socket
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as secured:
            return certificate_fingerprint(secured.getpeercert(binary_form=True))
