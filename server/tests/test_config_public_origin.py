"""VIBERPIT_PUBLIC_ORIGIN (MCP auth overhaul phase 4): the edge's https origin for OAuth
callbacks. Validated as ``https://host[:port]``; unset disables one-tap sign-in."""
from __future__ import annotations

import logging

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


def test_settings_reads_a_valid_origin(monkeypatch):
    monkeypatch.delenv("VIBERPIT_PUBLIC_ORIGIN", raising=False)
    assert get_settings().public_origin is None
    monkeypatch.setenv("VIBERPIT_PUBLIC_ORIGIN", "https://dobby.tail0dd570.ts.net/")
    assert get_settings().public_origin == "https://dobby.tail0dd570.ts.net"


def test_invalid_origin_degrades_to_unset_and_logs_one_error(monkeypatch, caplog):
    """The daemon shares the edge's env file: a typo must never crash it. It disables
    one-tap (paste-back everywhere) and logs one ERROR per process, not per call."""
    from server import config

    monkeypatch.setattr(config, "_public_origin_errors_logged", set())
    monkeypatch.setenv("VIBERPIT_PUBLIC_ORIGIN", "http://dobby.tail0dd570.ts.net")
    with caplog.at_level(logging.ERROR, logger="server.config"):
        assert get_settings().public_origin is None
        assert get_settings().public_origin is None
        assert config.public_origin_or_none("http://dobby.tail0dd570.ts.net") is None
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    msg = errors[0].getMessage()
    assert "VIBERPIT_PUBLIC_ORIGIN" in msg and "https://host[:port]" in msg
