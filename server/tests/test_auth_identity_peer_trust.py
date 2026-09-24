"""The identity header is only believed when the peer is entitled to assert it.

`Tailscale-User-Login` is stamped by the `tailscale serve` proxy. Its backend hop is
sourced from the host's OWN tailnet interface address, not loopback (#2134) — so a
genuinely stamped header can arrive with the peer set to loopback, to one of this
node's own tailscale addresses, or to an address in VIBERPIT_TRUSTED_PROXY_IPS. Any
other caller presenting that header is asserting an identity it was never given, so
the header must be ignored unless the peer is trusted.

An untrusted header is treated as *absent*, not as an error: it falls through to the
peer-verified `tailscale whois` path and then to the existing LAN-open / deny logic.
Hard-denying would break the documented `allow_unauthenticated` guest posture for any
client that happens to send the header.
"""
import ipaddress
import logging

import pytest
from starlette.requests import Request
from starlette.websockets import WebSocket

from server.auth.identity import OPEN_LAN_IDENTITY, verify_identity

IDENTITY = "tony@example.com"
LOOPBACK = ("127.0.0.1", 51000)
UNTRUSTED = ("203.0.113.7", 51000)
CONFIGURED_PROXY = ("10.4.4.4", 51000)
SELF_TAILSCALE_IP = ("100.102.11.121", 51000)


def _headers(**kv: str) -> list[tuple[bytes, bytes]]:
    return [(k.replace("_", "-").lower().encode(), v.encode()) for k, v in kv.items()]


def _http(client: tuple[str, int] | None, **headers: str) -> Request:
    """A real Starlette Request — no mock — with a chosen peer address."""
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/config",
        "headers": _headers(**headers),
        "client": client,
    })


def _ws(client: tuple[str, int] | None, **headers: str) -> WebSocket:
    return WebSocket({
        "type": "websocket",
        "path": "/ws",
        "headers": _headers(**headers),
        "client": client,
    }, receive=None, send=None)  # type: ignore[arg-type]


def _no_tailscale_binary(cmd, **kwargs):
    raise FileNotFoundError("no such file: tailscale")


@pytest.fixture(autouse=True)
def _locked_down(monkeypatch):
    """Deployed posture: an allowlist is set and LAN-open mode is off."""
    monkeypatch.setenv("VIBERPIT_TAILNET_ALLOWLIST", IDENTITY)
    monkeypatch.setenv("VIBERPIT_ALLOW_UNAUTHENTICATED", "false")
    monkeypatch.delenv("VIBERPIT_TRUSTED_PROXY_IPS", raising=False)
    # `tailscale whois` shells out to the host daemon; stub it so these tests exercise
    # the header-trust decision rather than the machine's real tailnet membership.
    monkeypatch.setattr("server.auth.identity._whois_identity", lambda request: None)
    # Same reasoning for the other shell-out in this module: `_self_tailscale_ips`
    # runs `tailscale status --json`. Default these tests to "this host has no
    # tailscale" so a peer is trusted only for the reason under test; the cases that
    # care re-stub it below. (The cache it fills is reset per-test by conftest.)
    monkeypatch.setattr(
        "server.auth.identity.subprocess.run",
        _no_tailscale_binary,
    )


def test_header_from_loopback_peer_is_accepted():
    """The real topology: tailscale serve proxies to 127.0.0.1 and stamps the header."""
    assert verify_identity(_http(LOOPBACK, Tailscale_User_Login=IDENTITY)) == IDENTITY


def test_header_from_untrusted_peer_is_rejected():
    """A caller that reaches the edge directly cannot assert an allowlisted identity."""
    assert verify_identity(_http(UNTRUSTED, Tailscale_User_Login=IDENTITY)) is None


def test_alternate_header_name_from_untrusted_peer_is_rejected():
    """The X- spelling is the same assertion and gets the same treatment."""
    assert verify_identity(_http(UNTRUSTED, X_Tailscale_User_Login=IDENTITY)) is None


def test_header_from_configured_trusted_proxy_is_accepted(monkeypatch):
    monkeypatch.setenv("VIBERPIT_TRUSTED_PROXY_IPS", "10.4.4.4, 10.5.5.5")
    assert verify_identity(_http(CONFIGURED_PROXY, Tailscale_User_Login=IDENTITY)) == IDENTITY


def test_header_from_this_nodes_own_tailscale_ip_is_accepted(monkeypatch):
    """`tailscale serve`'s backend hop presents as the host's own tailnet IP (#2134),
    with no VIBERPIT_TRUSTED_PROXY_IPS entry required."""
    monkeypatch.setattr(
        "server.auth.identity._self_tailscale_ips",
        lambda: frozenset({ipaddress.ip_address(SELF_TAILSCALE_IP[0])}),
    )
    assert verify_identity(_http(SELF_TAILSCALE_IP, Tailscale_User_Login=IDENTITY)) == IDENTITY


def test_header_from_an_untrusted_peer_that_merely_resembles_a_self_ip_is_rejected(monkeypatch):
    """The self-IP allowance is exact-match, not a rubber stamp for anything tailnet-shaped."""
    monkeypatch.setattr(
        "server.auth.identity._self_tailscale_ips",
        lambda: frozenset({ipaddress.ip_address("100.102.11.121")}),
    )
    assert verify_identity(_http(UNTRUSTED, Tailscale_User_Login=IDENTITY)) is None


def test_untrusted_header_is_ignored_not_fatal_in_lan_open_mode(monkeypatch):
    """LAN-open admits the caller as a guest — it must not inherit the spoofed identity."""
    monkeypatch.setenv("VIBERPIT_ALLOW_UNAUTHENTICATED", "true")
    assert verify_identity(_http(UNTRUSTED, Tailscale_User_Login=IDENTITY)) == OPEN_LAN_IDENTITY


def test_request_without_a_peer_address_is_rejected():
    """No client tuple means no way to establish trust — fail closed."""
    assert verify_identity(_http(None, Tailscale_User_Login=IDENTITY)) is None


def test_websocket_handshake_applies_the_same_peer_check():
    assert verify_identity(_ws(UNTRUSTED, Tailscale_User_Login=IDENTITY)) is None
    assert verify_identity(_ws(LOOPBACK, Tailscale_User_Login=IDENTITY)) == IDENTITY


def test_untrusted_header_is_logged(caplog):
    """A presented-but-untrusted header is the attack signal — it must be visible."""
    with caplog.at_level(logging.WARNING, logger="server.auth.identity"):
        verify_identity(_http(UNTRUSTED, Tailscale_User_Login=IDENTITY))
    assert any("203.0.113.7" in r.getMessage() for r in caplog.records), (
        "expected a warning naming the untrusted peer"
    )


# --- address handling (adopted from the parallel work on #1131, PR #1142) ----------
# Loopback is the whole 127.0.0.0/8 block, not just the .1 host, and the trusted-proxy
# list takes CIDR ranges. `ipaddress` decides both — with one wrinkle: it reports
# is_loopback=False for the IPv4-mapped form, so that must be unwrapped first.


def test_any_address_in_the_loopback_block_is_trusted():
    assert verify_identity(_http(("127.0.0.5", 5), Tailscale_User_Login=IDENTITY)) == IDENTITY


def test_ipv6_loopback_is_trusted():
    assert verify_identity(_http(("::1", 5), Tailscale_User_Login=IDENTITY)) == IDENTITY


def test_ipv4_mapped_loopback_is_trusted():
    """ipaddress reports is_loopback=False here; the mapped v4 address must be unwrapped."""
    assert verify_identity(
        _http(("::ffff:127.0.0.1", 5), Tailscale_User_Login=IDENTITY)
    ) == IDENTITY


def test_trusted_proxy_list_accepts_a_cidr_range(monkeypatch):
    monkeypatch.setenv("VIBERPIT_TRUSTED_PROXY_IPS", "10.0.0.0/24")
    assert verify_identity(_http(("10.0.0.77", 5), Tailscale_User_Login=IDENTITY)) == IDENTITY
    assert verify_identity(_http(("10.0.1.77", 5), Tailscale_User_Login=IDENTITY)) is None


def test_unparseable_peer_host_is_not_trusted():
    """Fail closed. A peer that is not an IP cannot be placed inside any trusted range."""
    assert verify_identity(_http(("testclient", 5), Tailscale_User_Login=IDENTITY)) is None


def test_malformed_entry_in_the_proxy_list_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setenv("VIBERPIT_TRUSTED_PROXY_IPS", "not-an-ip, 10.0.0.0/24")
    assert verify_identity(_http(("10.0.0.77", 5), Tailscale_User_Login=IDENTITY)) == IDENTITY


# --- _self_tailscale_ips (#2134): resolves this node's own tailnet addresses so a
# `tailscale serve` backend hop, which presents as that address rather than loopback,
# is trusted without per-host VIBERPIT_TRUSTED_PROXY_IPS config. ------------------------


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout


# Each of these starts against a cold cache: `server/tests/conftest.py` resets the
# module global around every test in the suite, so no local fixture is needed here
# — and, more to the point, no OTHER test file inherits a fake verdict latched by
# one of these.


def test_self_tailscale_ips_parses_status_json(monkeypatch):
    from server.auth import identity

    def _fake_run(cmd, **kwargs):
        assert cmd[:2] == ["tailscale", "status"]
        return _FakeCompletedProcess(0, '{"Self": {"TailscaleIPs": ["100.102.11.121", "fd7a::1"]}}')

    monkeypatch.setattr(identity.subprocess, "run", _fake_run)
    ips = identity._self_tailscale_ips()
    assert ipaddress.ip_address("100.102.11.121") in ips
    assert ipaddress.ip_address("fd7a::1") in ips


def test_self_tailscale_ips_is_empty_when_tailscale_is_unavailable(monkeypatch):
    from server.auth import identity

    def _fake_run(cmd, **kwargs):
        raise FileNotFoundError("no such file: tailscale")

    monkeypatch.setattr(identity.subprocess, "run", _fake_run)
    assert identity._self_tailscale_ips() == frozenset()


def test_self_tailscale_ips_is_empty_on_nonzero_exit(monkeypatch):
    from server.auth import identity

    monkeypatch.setattr(identity.subprocess, "run", lambda cmd, **kw: _FakeCompletedProcess(1, ""))
    assert identity._self_tailscale_ips() == frozenset()


def test_self_tailscale_ips_result_is_cached_across_calls(monkeypatch):
    from server.auth import identity

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(1)
        return _FakeCompletedProcess(0, '{"Self": {"TailscaleIPs": ["100.102.11.121"]}}')

    monkeypatch.setattr(identity.subprocess, "run", _fake_run)
    first = identity._self_tailscale_ips()
    second = identity._self_tailscale_ips()
    assert first == second == frozenset({ipaddress.ip_address("100.102.11.121")})
    assert len(calls) == 1, "second call within the TTL must not re-spawn the subprocess"
