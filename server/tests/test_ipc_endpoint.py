import os
from unittest import mock

import pytest

from server.ipc.paths import (
    DEFAULT_SOCKET_PATH, Endpoint, parse_endpoint, sessiond_endpoint, sessiond_socket,
)


def test_parse_unix_scheme():
    ep = parse_endpoint("unix:///run/ccswitch/sessiond.sock")
    assert ep.is_unix and ep.path == "/run/ccswitch/sessiond.sock"


def test_parse_bare_path_is_unix():
    ep = parse_endpoint("/tmp/x.sock")
    assert ep.is_unix and ep.path == "/tmp/x.sock"


def test_parse_https_scheme():
    ep = parse_endpoint("https://daemon.host:9443")
    assert ep.is_tcp and ep.host == "daemon.host" and ep.port == 9443


def test_parse_https_requires_host_and_port():
    with pytest.raises(ValueError):
        parse_endpoint("https://daemon.host")


def test_parse_rejects_unknown_scheme():
    with pytest.raises(ValueError):
        parse_endpoint("ftp://nope")


def test_sessiond_endpoint_precedence():
    # ENDPOINT wins over legacy SOCKET.
    with mock.patch.dict(os.environ, {
        "CCSWITCH_SESSIOND_ENDPOINT": "https://h:1",
        "CCSWITCH_SESSIOND_SOCKET": "/tmp/legacy.sock",
    }):
        assert sessiond_endpoint() == Endpoint("https", host="h", port=1)
    # legacy SOCKET (bare path) maps to unix://.
    env = {k: v for k, v in os.environ.items()
           if k not in ("CCSWITCH_SESSIOND_ENDPOINT", "CCSWITCH_SESSIOND_SOCKET")}
    with mock.patch.dict(os.environ, {**env, "CCSWITCH_SESSIOND_SOCKET": "/tmp/legacy.sock"}, clear=True):
        assert sessiond_endpoint() == Endpoint("unix", path="/tmp/legacy.sock")
    # nothing set → default unix socket.
    with mock.patch.dict(os.environ, env, clear=True):
        assert sessiond_endpoint() == Endpoint("unix", path=DEFAULT_SOCKET_PATH)


def test_sessiond_socket_unchanged():
    env = {k: v for k, v in os.environ.items() if k != "CCSWITCH_SESSIOND_SOCKET"}
    with mock.patch.dict(os.environ, env, clear=True):
        assert sessiond_socket() == DEFAULT_SOCKET_PATH
