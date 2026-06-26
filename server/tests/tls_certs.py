"""Test-only TLS cert minting (cryptography). Fixed validity dates → no clock dependency."""
from __future__ import annotations

import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_NOT_BEFORE = datetime.datetime(2020, 1, 1)
_NOT_AFTER = datetime.datetime(2099, 1, 1)


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_ca():
    key = _key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ccswitch-test-ca")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOT_BEFORE).not_valid_after(_NOT_AFTER)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def make_leaf(ca_key, ca_cert, common_name, *, san_dns=None, san_ip=None):
    key = _key()
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_NOT_BEFORE).not_valid_after(_NOT_AFTER)
    )
    san = [x509.DNSName(d) for d in (san_dns or [])]
    san += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in (san_ip or [])]
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName(san), critical=False)
    return key, builder.sign(ca_key, hashes.SHA256())


def write_pem_cert(path: Path, cert) -> None:
    Path(path).write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def write_pem_key(path: Path, key) -> None:
    Path(path).write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
