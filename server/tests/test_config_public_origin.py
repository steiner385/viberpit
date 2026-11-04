"""VIBERPIT_PUBLIC_ORIGIN (MCP auth overhaul phase 4): the edge's https origin for OAuth
callbacks. Validated as ``https://host[:port]``; unset disables one-tap sign-in."""
from __future__ import annotations

import pytest

from server.config import get_settings, parse_public_origin


@pytest.mark.parametrize(("raw", "want"), [
    ("https://dobby.tail0dd570.ts.net", "https://dobby.tail0dd570.ts.net"),
    ("https://dobby.tail0dd570.ts.net/", "https://dobby.tail0dd570.ts.net"),
    ("https://Dobby.Example.ts.net:8443", "https://dobby.example.ts.net:8443"),
    ("  https://dobby.example  ", "https://dobby.example"),
    (None, None),
    ("", None),
    ("   ", None),
])
def test_accepts_https_origins(raw, want):
    assert parse_public_origin(raw) == want


@pytest.mark.parametrize("raw", [
    "http://dobby.tail0dd570.ts.net",          # plain http
    "https://dobby.tail0dd570.ts.net/mcp",     # a path
    "https://dobby.example//",                 # a path, not a bare slash
    "https://dobby.example?x=1",               # a query
    "https://dobby.example/?x=1",
    "https://dobby.example#frag",              # a fragment
    "https://user:pw@dobby.example",           # userinfo
    "https://dobby.example:0",                 # bad port
    "https://dobby.example:99999",
    "https://dobby.example:abc",
    "https://",                                # no host
    "dobby.example",                           # no scheme
    "ftp://dobby.example",
    "not a url",
    "https://dob by.example",
])
def test_rejects_anything_but_an_https_origin(raw):
    with pytest.raises(ValueError):
        parse_public_origin(raw)


def test_settings_reads_and_validates_the_env(monkeypatch):
    monkeypatch.delenv("VIBERPIT_PUBLIC_ORIGIN", raising=False)
    assert get_settings().public_origin is None
    monkeypatch.setenv("VIBERPIT_PUBLIC_ORIGIN", "https://dobby.tail0dd570.ts.net/")
    assert get_settings().public_origin == "https://dobby.tail0dd570.ts.net"
    monkeypatch.setenv("VIBERPIT_PUBLIC_ORIGIN", "http://dobby.tail0dd570.ts.net")
    with pytest.raises(ValueError):   # an invalid value fails startup, never silently disables
        get_settings()
