import pytest
from server.ipc.address import parse_daemon_url, daemon_address
from server.ipc.paths import DEFAULT_SOCKET_PATH


def test_parse_unix_url():
    a = parse_daemon_url("unix:///run/ccswitch/sessiond.sock")
    assert a.kind == "unix" and a.uds_path == "/run/ccswitch/sessiond.sock"
    assert a.uvicorn_kwargs() == {"uds": "/run/ccswitch/sessiond.sock"}
    assert a.httpx_base_url() == "http://daemon"
    assert a.ws_uri() == "ws://daemon/events"


def test_parse_tcp_url_v4():
    a = parse_daemon_url("tcp://100.64.0.5:8788")
    assert a.kind == "tcp" and a.host == "100.64.0.5" and a.port == 8788
    assert a.uvicorn_kwargs() == {"host": "100.64.0.5", "port": 8788}
    assert a.httpx_base_url() == "http://100.64.0.5:8788"
    assert a.ws_uri() == "ws://100.64.0.5:8788/events"


def test_parse_tcp_url_v6_bracketed():
    a = parse_daemon_url("tcp://[fd7a::5]:8788")
    assert a.kind == "tcp" and a.host == "fd7a::5" and a.port == 8788
    assert a.httpx_base_url() == "http://[fd7a::5]:8788"
    assert a.ws_uri() == "ws://[fd7a::5]:8788/events"


@pytest.mark.parametrize("url", ["tcp://0.0.0.0:8788", "tcp://[::]:8788", "tcp://:8788"])
def test_wildcard_host_rejected(url):
    with pytest.raises(ValueError, match="wildcard|specific"):
        parse_daemon_url(url)


@pytest.mark.parametrize("url", ["tcp://100.64.0.5", "tcp://100.64.0.5:0"])
def test_missing_or_zero_port_rejected(url):
    with pytest.raises(ValueError):
        parse_daemon_url(url)


def test_unknown_scheme_rejected():
    with pytest.raises(ValueError, match="scheme"):
        parse_daemon_url("https://100.64.0.5:8788")


def test_unix_url_needs_path():
    with pytest.raises(ValueError):
        parse_daemon_url("unix://")


def test_daemon_address_unset_defaults_to_uds(monkeypatch):
    monkeypatch.delenv("CCSWITCH_SESSIOND_URL", raising=False)
    monkeypatch.delenv("CCSWITCH_SESSIOND_SOCKET", raising=False)
    a = daemon_address()
    assert a.kind == "unix" and a.uds_path == DEFAULT_SOCKET_PATH


def test_daemon_address_socket_env_backcompat(monkeypatch):
    monkeypatch.delenv("CCSWITCH_SESSIOND_URL", raising=False)
    monkeypatch.setenv("CCSWITCH_SESSIOND_SOCKET", "/tmp/x.sock")
    a = daemon_address()
    assert a.kind == "unix" and a.uds_path == "/tmp/x.sock"


def test_daemon_address_url_wins_over_socket(monkeypatch, caplog):
    monkeypatch.setenv("CCSWITCH_SESSIOND_URL", "tcp://100.64.0.5:8788")
    monkeypatch.setenv("CCSWITCH_SESSIOND_SOCKET", "/tmp/x.sock")
    import logging
    with caplog.at_level(logging.WARNING):
        a = daemon_address()
    assert a.kind == "tcp" and a.host == "100.64.0.5"
    assert any("CCSWITCH_SESSIOND_URL" in r.message for r in caplog.records)
