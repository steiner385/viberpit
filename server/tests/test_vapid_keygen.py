"""VAPID keypair generation for self-hosted Web Push."""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from server.sessions.push import generate_vapid_keys


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_generate_vapid_keys_shapes():
    priv_pem, app_server_key = generate_vapid_keys()
    assert priv_pem.startswith("-----BEGIN PRIVATE KEY-----")
    raw = _b64url_decode(app_server_key)
    # Uncompressed P-256 point: 65 bytes, leading 0x04.
    assert len(raw) == 65
    assert raw[0] == 0x04
    assert "=" not in app_server_key  # base64url, unpadded (browser applicationServerKey)


def test_generate_vapid_keys_unique():
    assert generate_vapid_keys()[0] != generate_vapid_keys()[0]
