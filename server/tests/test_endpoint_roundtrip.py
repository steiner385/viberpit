"""Endpoint must round-trip through str(); the default repr is not a URL.

ensure_default_host stored str(endpoint) as the fleet host's endpoint, and with
only the dataclass default that produced

    "Endpoint(scheme='https', path=None, host='rocky...', port=9443)"

which parse_endpoint rejects. The registry's only host was therefore permanently
unhealthy with 'ValueError: unsupported daemon endpoint' (observed in production
2026-08-22, immediately after #720 deployed).

A __str__ that is the exact inverse of parse_endpoint makes the mistake class
impossible rather than fixing the one call site.
"""
from __future__ import annotations

import pytest

from server.ipc.paths import parse_endpoint


@pytest.mark.parametrize("raw", [
    "unix:///run/ccswitch/sessiond.sock",
    "https://rocky.tail0dd570.ts.net:9443",
    "https://127.0.0.1:9443",
])
def test_parse_str_roundtrip(raw):
    ep = parse_endpoint(raw)
    assert str(ep) == raw, "str() must be the inverse of parse_endpoint"
    assert parse_endpoint(str(ep)) == ep


def test_str_is_not_the_dataclass_repr():
    """The bug: str() fell through to repr, which parse_endpoint cannot read."""
    ep = parse_endpoint("https://rocky:9443")
    assert not str(ep).startswith("Endpoint("), "str() must not be the repr"
    assert str(ep) == "https://rocky:9443"


def test_stored_endpoint_is_reparseable():
    """What ensure_default_host actually needs: store it, read it back, use it."""
    for raw in ("unix:///tmp/x.sock", "https://h:9443"):
        stored = str(parse_endpoint(raw))
        assert parse_endpoint(stored).scheme in ("unix", "https")
