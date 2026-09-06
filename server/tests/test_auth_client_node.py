"""client_node_name: which tailnet machine the browser is on (spec §4.1).

Same peer-trust rule as identity: X-Real-IP is only honoured from loopback /
CCSWITCH_TRUSTED_PROXY_IPS, and the whois subprocess is the only source of truth.
"""
import json
from unittest.mock import MagicMock

from starlette.requests import Request

from server.auth.identity import client_node_name

LOOPBACK = ("127.0.0.1", 51000)
UNTRUSTED = ("203.0.113.7", 51000)


def _headers(**kv: str) -> list[tuple[bytes, bytes]]:
    return [(k.replace("_", "-").lower().encode(), v.encode()) for k, v in kv.items()]


def _http(client, **headers: str) -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/sessions",
                    "headers": _headers(**headers), "client": client})


def _whois_ok(node_name="dobby"):
    def fake(cmd, **kw):
        fake.calls.append(cmd)
        m = MagicMock()
        m.returncode = 0
        m.stdout = json.dumps({
            "Node": {"Name": f"{node_name}.tail0dd570.ts.net.", "ComputedName": node_name,
                     "Addresses": ["100.69.70.109/32"]},
            "UserProfile": {"LoginName": "tony@example.com"},
        })
        return m
    fake.calls = []
    return fake


def test_resolves_node_from_x_real_ip_behind_loopback_proxy(monkeypatch):
    fake = _whois_ok("Dobby")
    monkeypatch.setattr("server.auth.identity.subprocess.run", fake)
    node = client_node_name(_http(LOOPBACK, x_real_ip="100.69.70.109"))
    assert node == "dobby"                      # lower-cased
    assert fake.calls[0][-1] == "100.69.70.109"  # the forwarded peer, after "--"


def test_untrusted_peer_cannot_forward_a_real_ip(monkeypatch):
    fake = _whois_ok()
    monkeypatch.setattr("server.auth.identity.subprocess.run", fake)
    client_node_name(_http(UNTRUSTED, x_real_ip="100.69.70.109"))
    assert fake.calls[0][-1] == "203.0.113.7"   # whois runs on the direct peer instead


def test_loopback_without_forwarded_ip_is_unknown(monkeypatch):
    """`curl 127.0.0.1` on the host must never count as 'local': whois has no peer."""
    def fail(cmd, **kw):
        m = MagicMock(); m.returncode = 1; m.stdout = ""; return m
    monkeypatch.setattr("server.auth.identity.subprocess.run", fail)
    assert client_node_name(_http(LOOPBACK)) is None


def test_whois_failure_is_none_not_an_error(monkeypatch):
    def boom(cmd, **kw):
        raise OSError("tailscale not installed")
    monkeypatch.setattr("server.auth.identity.subprocess.run", boom)
    assert client_node_name(_http(LOOPBACK, x_real_ip="100.1.1.1")) is None


def test_non_ip_forwarded_value_never_reaches_argv(monkeypatch):
    fake = _whois_ok()
    monkeypatch.setattr("server.auth.identity.subprocess.run", fake)
    assert client_node_name(_http(LOOPBACK, x_real_ip="--flag")) is None
    assert fake.calls == []


def test_missing_computed_name_is_none(monkeypatch):
    def fake(cmd, **kw):
        m = MagicMock(); m.returncode = 0
        m.stdout = json.dumps({"UserProfile": {"LoginName": "x@y"}}); return m
    monkeypatch.setattr("server.auth.identity.subprocess.run", fake)
    assert client_node_name(_http(LOOPBACK, x_real_ip="100.1.1.1")) is None


def test_dependency_returns_none_without_raising():
    """current_client_node is a soft dependency: no 401, just None."""
    from server.auth.middleware import current_client_node
    req = _http(("testclient", 1))
    assert current_client_node(req) is None
