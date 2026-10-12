"""A process on this host that asserts the operator's Tailscale identity is not the UI (#2980).

``tailscale serve`` and a session's ``curl 127.0.0.1:8787`` both reach the edge from a local
address with a ``Tailscale-User-Login`` header, so peer IP cannot separate them. The owner of
the client socket can: tailscaled runs as root (nginx as www-data), a session as the operator's
user. And the origin the front-end stamps says whether a local process went THROUGH it.
``identity.human_gate_via`` turns anything else into ``via="local"``, which every human-only
gate (always-allow rule writes, profile-pattern writes) refuses. The socket peer is the one
``TransportPeerMiddleware`` recorded, never ``scope["client"]``, which X-Forwarded-For
rewrites. Threat note: docs/security/2026-10-11-local-identity-assertion.md.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import struct
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from server.auth import identity
from server.auth.identity import (
    LOCAL_PROCESS_VIA, TRANSPORT_PEER_SCOPE_KEY, TransportPeerMiddleware, human_gate_via,
    peer_socket_uids,
)
from server.tests.test_permission_rules_api import (
    _ENV, _H, S1, _always, _card, _make_client, _rules, _seed_sessions,
)
from server.tests.test_ws import _IDENTITY_HEADERS, _drain_events, ws_env  # noqa: F401

_UIDS = "server.auth.identity.peer_socket_uids"
_OPERATOR_UID = 1000


# --- the socket-owner lookup, against real sockets -------------------------------------

@pytest.mark.parametrize("family,host", [(socket.AF_INET, "127.0.0.1"),
                                         (socket.AF_INET6, "::1")])
def test_peer_socket_uids_finds_a_local_client_socket_and_its_owner(family, host):
    with socket.socket(family) as srv:
        srv.bind((host, 0))
        srv.listen()
        with socket.socket(family) as cli:
            cli.connect(srv.getsockname()[:2])
            conn, peer = srv.accept()
            with conn:
                assert peer_socket_uids(peer[:2], conn.getsockname()[:2],
                                        _ino(conn)) == {os.getuid()}


def test_peer_socket_uids_matches_a_v4_client_of_a_dual_stack_listener():
    with socket.socket(socket.AF_INET6) as srv:
        srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        srv.bind(("::", 0))
        srv.listen()
        with socket.socket(socket.AF_INET) as cli:
            cli.connect(("127.0.0.1", srv.getsockname()[1]))
            conn, peer = srv.accept()
            with conn:
                assert peer[0] == "::ffff:127.0.0.1"
                assert peer_socket_uids(peer[:2], conn.getsockname()[:2],
                                        _ino(conn)) == {os.getuid()}


def _ino(sock):
    return os.fstat(sock.fileno()).st_ino


def test_peer_socket_uids_is_none_without_the_edges_own_live_socket():
    # No established server row with this inode: the connection is gone (or never was),
    # so its tuple cannot be attributed to anyone.
    assert peer_socket_uids(("100.64.0.9", 5555), ("127.0.0.1", 8787), 12345) is None
    assert peer_socket_uids(("127.0.0.1", 5555), ("127.0.0.1", 8787), None) is None
    assert peer_socket_uids(("testclient", 50000), ("127.0.0.1", 8787), 1) is None


def test_peer_socket_uids_binds_the_tuple_to_the_edges_socket_inode():
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        cli = socket.socket()
        cli.connect(srv.getsockname())
        conn, peer = srv.accept()
        with conn:
            assert peer_socket_uids(peer, conn.getsockname(), _ino(conn)) == {os.getuid()}
            assert peer_socket_uids(peer, conn.getsockname(), _ino(conn) + 1) is None
            # A reset frees the 4-tuple at once; a new connection could take it before the
            # handler looks. The edge's own row is gone, so nothing is attributed.
            cli.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            cli.close()
            deadline = time.monotonic() + 2.0
            uids = peer_socket_uids(peer, conn.getsockname(), _ino(conn))
            while uids is not None and time.monotonic() < deadline:
                time.sleep(0.01)
                uids = peer_socket_uids(peer, conn.getsockname(), _ino(conn))
            assert uids is None


def test_peer_socket_uids_never_attributes_a_timewait_client_row(tmp_path, monkeypatch):
    # The kernel prints a closed client's timewait minisocket with uid 0 and inode 0.
    # Even beside a live edge row it must not read as tailscaled.
    hdr = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    rows = ("   0: 0100007F:2253 0100007F:9C40 01 00000000:00000000 00:00000000 00000000  1000        0 777\n"
            "   1: 0100007F:9C40 0100007F:2253 05 00000000:00000000 03:00001000 00000000     0        0 0\n")
    table = tmp_path / "tcp"
    table.write_text(hdr + rows)
    monkeypatch.setattr(identity, "_PROC_NET_TCP", (str(table),))
    assert peer_socket_uids(("127.0.0.1", 40000), ("127.0.0.1", 8787), 777) == {-1}
    table.write_text(hdr + rows.replace(" 05 ", " 01 ").replace("     0        0 0\n",
                                                                  "     0        0 888\n"))
    assert peer_socket_uids(("127.0.0.1", 40000), ("127.0.0.1", 8787), 777) == {0}


# --- the gate's decision table ---------------------------------------------------------

_REAL_ATTEST = identity.attested_front_end_hop
_OWN = "100.64.0.1"        # this node's tailnet address
_PHONE = "100.64.0.9"      # the operator on another tailnet device
_FRONT_ENDS = {0: "x-forwarded-for", 33: "x-real-ip"}   # tailscaled, nginx (www-data)


@pytest.fixture(autouse=True)
def _real_attestation(monkeypatch):
    # conftest stubs the attestation for every other suite; this file tests the real one.
    import ipaddress
    monkeypatch.setattr(identity, "attested_front_end_hop", _REAL_ATTEST)
    monkeypatch.setattr(identity, "_front_end_headers", lambda: dict(_FRONT_ENDS))
    monkeypatch.setattr(identity, "_self_tailscale_ips",
                        lambda: frozenset({ipaddress.ip_address(_OWN)}))


def _conn(host, port=50000, via=None, headers=None, recorded=True):
    scope = {"type": "http", "method": "POST", "path": "/", "client": (host, port),
             "server": ("127.0.0.1", 8787),
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    if recorded:
        scope[TRANSPORT_PEER_SCOPE_KEY] = (host, port)
    if via is not None:
        scope["state"] = {"via": via}
    return Request(scope)


_XFF, _XRI = "X-Forwarded-For", "X-Real-IP"


@pytest.mark.parametrize("host,uids,headers,expected", [
    # a session's curl, with or without a forged origin header
    ("127.0.0.1", {_OPERATOR_UID}, {}, LOCAL_PROCESS_VIA),
    ("127.0.0.1", {_OPERATOR_UID}, {_XRI: _PHONE}, LOCAL_PROCESS_VIA),
    # tailscale serve relaying the phone; a local curl THROUGH serve (confused deputy)
    ("127.0.0.1", {0}, {_XFF: _PHONE}, None),
    ("::ffff:127.0.0.1", {0}, {_XFF: _PHONE}, None),
    ("127.0.0.1", {0}, {_XFF: _OWN}, LOCAL_PROCESS_VIA),
    ("127.0.0.1", {0}, {_XFF: "127.0.0.1"}, LOCAL_PROCESS_VIA),
    ("127.0.0.1", {0}, {_XFF: f"{_PHONE}, {_OWN}"}, LOCAL_PROCESS_VIA),  # forged prefix
    ("127.0.0.1", {0}, {}, LOCAL_PROCESS_VIA),                # no origin stamped
    ("127.0.0.1", {0}, {_XRI: _PHONE}, LOCAL_PROCESS_VIA),    # serve's header is XFF
    # nginx relaying the phone; a local curl through nginx, incl. via a LAN/docker address
    ("127.0.0.1", {33}, {_XRI: _PHONE}, None),
    ("127.0.0.1", {33}, {_XRI: "8.8.8.8"}, LOCAL_PROCESS_VIA),   # not a tailnet address
    ("127.0.0.1", {33}, {_XRI: _OWN}, LOCAL_PROCESS_VIA),
    ("127.0.0.1", {33}, {_XRI: "127.0.0.1"}, LOCAL_PROCESS_VIA),
    ("127.0.0.1", {33}, {_XFF: _PHONE}, LOCAL_PROCESS_VIA),   # nginx passes XFF through
    # unattributable sockets
    ("127.0.0.1", {0, 33}, {_XFF: _PHONE, _XRI: _PHONE}, LOCAL_PROCESS_VIA),
    ("127.0.0.1", {-1}, {_XFF: _PHONE}, LOCAL_PROCESS_VIA),   # timewait / orphaned
    ("127.0.0.1", set(), {}, LOCAL_PROCESS_VIA),              # loopback, no socket
    ("127.0.0.1", None, {}, LOCAL_PROCESS_VIA),               # tables unreadable
    # a peer with no socket on this host
    (_PHONE, set(), {}, None),
    (_OWN, set(), {}, LOCAL_PROCESS_VIA),
    (_OWN, {_OPERATOR_UID}, {}, LOCAL_PROCESS_VIA),
])
def test_human_gate_via(host, uids, headers, expected):
    with patch(_UIDS, return_value=uids):
        assert human_gate_via(_conn(host, headers=headers)) == expected


def test_an_origin_this_host_owns_is_local_even_off_the_tailnet():
    # nginx listens on 0.0.0.0, so a local process can reach it through a LAN or docker
    # address. Whatever address that is, this host can bind it.
    with patch(_UIDS, return_value={33}):
        for lan in ("127.0.0.1", "0.0.0.0"):
            assert human_gate_via(_conn("127.0.0.1", headers={_XRI: lan})) == LOCAL_PROCESS_VIA
    assert identity._is_this_host("127.0.0.2")
    assert not identity._is_this_host(_PHONE)


def test_is_this_host_recognises_a_non_loopback_interface_address():
    # The bind probe: the address this host would source a packet from is its own.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))   # TEST-NET-1; UDP connect sends nothing
        except OSError:
            pytest.skip("no IPv4 route on this host")
        lan = probe.getsockname()[0]
    if ipaddress.ip_address(lan).is_loopback:
        pytest.skip("only loopback here")
    assert identity._is_this_host(lan)


def test_human_gate_via_fails_closed_when_this_nodes_addresses_are_unknown(monkeypatch):
    # `tailscale status` failed: an origin cannot be shown not to be this node.
    monkeypatch.setattr(identity, "_self_tailscale_ips", lambda: frozenset())
    with patch(_UIDS, return_value={0}):
        assert human_gate_via(_conn("127.0.0.1", headers={_XFF: _PHONE})) == LOCAL_PROCESS_VIA


@pytest.mark.parametrize("peer,xff", [
    (("100.64.0.9", 0), "100.64.0.9"),            # uvicorn rewrote it: no port
    (("127.0.0.1", 40000), "127.0.0.1:40000"),    # rewritten to a chosen local socket
    (("::1", 40000), "1.2.3.4, [::1]:40000"),
])
def test_human_gate_via_fails_closed_when_the_recorded_peer_came_from_x_forwarded_for(
        peer, xff):
    # An older unit still running uvicorn's own proxy headers rewrites scope["client"]
    # BEFORE TransportPeerMiddleware records it, so the recorded peer is a header value a
    # local process chose. Even one naming a root-owned socket must not attest.
    with patch(_UIDS, return_value={0}):
        assert human_gate_via(_conn(*peer, headers={_XFF: xff})) == LOCAL_PROCESS_VIA


def test_human_gate_via_fails_closed_without_a_recorded_transport_peer():
    with patch(_UIDS, return_value={0}):
        assert human_gate_via(_conn("127.0.0.1", headers={_XFF: _PHONE},
                                    recorded=False)) == LOCAL_PROCESS_VIA


def test_transport_peer_middleware_records_the_socket_peer_and_still_applies_xff():
    seen = {}

    async def app(scope, receive, send):
        seen.update(client=scope["client"], peer=scope[TRANSPORT_PEER_SCOPE_KEY],
                    inode=scope[identity.TRANSPORT_INODE_SCOPE_KEY])

    mw = TransportPeerMiddleware(app)
    scope = {"type": "http", "client": ("127.0.0.1", 41000), "scheme": "http",
             "headers": [(b"x-forwarded-for", b"100.64.0.9")]}
    import asyncio
    asyncio.run(mw(scope, None, None))
    # request.client is rewritten exactly as uvicorn did (the token gates rely on it);
    # the recorded peer is the socket.
    # No uvicorn transport behind this receive: no inode, so the gate fails closed.
    assert seen == {"client": ("100.64.0.9", 0), "peer": ("127.0.0.1", 41000), "inode": None}


_ATTACKER = "100.64.0.66"  # another tailnet device, not the operator


def _whois_target(uids, headers):
    """The address `tailscale whois` is asked about for a request that reached the edge
    on 127.0.0.1:41000 with *headers*, the socket owned by *uids*, after the real
    TransportPeerMiddleware applied X-Forwarded-For."""
    import asyncio
    seen = {}

    async def app(scope, receive, send):
        seen["request"] = Request(scope)

    scope = {"type": "http", "client": ("127.0.0.1", 41000), "server": ("127.0.0.1", 8787),
             "scheme": "http", "path": "/", "method": "GET",
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    asyncio.run(TransportPeerMiddleware(app)(scope, None, None))
    argv = []

    def run(cmd, **_kw):
        argv.append(cmd[-1])
        return type("R", (), {"returncode": 1, "stdout": ""})()

    lookup = {"side_effect": uids} if isinstance(uids, BaseException) else {"return_value": uids}
    with patch(_UIDS, **lookup), patch.object(identity.subprocess, "run", run):
        identity._whois(seen["request"])
    return argv[0] if argv else None


@pytest.mark.parametrize("uids,headers,target", [
    # nginx overwrote X-Real-IP with the attacker's address and passed their forged
    # X-Forwarded-For through (an unpatched vhost): whois must ask about the attacker.
    ({33}, {_XFF: _PHONE, _XRI: _ATTACKER}, _ATTACKER),
    ({33}, {_XFF: f"{_ATTACKER}, {_PHONE}", _XRI: _ATTACKER}, _ATTACKER),
    ({33}, {_XFF: _ATTACKER, _XRI: _ATTACKER}, _ATTACKER),  # the patched vhost
    # tailscale serve sets X-Forwarded-For itself; a client X-Real-IP is not its header.
    ({0}, {_XFF: _PHONE, _XRI: _ATTACKER}, _PHONE),
    # a socket no front-end owns, or one that cannot be attributed, names no one
    ({_OPERATOR_UID}, {_XFF: _PHONE}, None),
    (None, {_XFF: _PHONE, _XRI: _PHONE}, None),
    ({33, 0}, {_XFF: _PHONE, _XRI: _PHONE}, None),
])
def test_a_forged_x_forwarded_for_cannot_pick_the_whois_target(uids, headers, target):
    # #3010: request.client is rewritten from X-Forwarded-For, which only tailscaled sets.
    assert _whois_target(uids, headers) == target


def test_whois_without_a_rewrite_still_honours_x_real_ip_from_a_trusted_peer():
    assert _whois_target(AssertionError("no rewrite: must not read /proc"),
                         {_XRI: _PHONE}) == _PHONE


def test_human_gate_via_keeps_recorded_attribution_and_skips_non_ip_transports():
    with patch(_UIDS, side_effect=AssertionError("must not read /proc")):
        assert human_gate_via(_conn("127.0.0.1", via="assistant")) == "assistant"
        assert human_gate_via(_conn("testclient")) is None


# --- every write route refuses a local process, admits the serve hop ------------------

def _local_client(client):
    return TestClient(client.app, client=("127.0.0.1", 50000), raise_server_exceptions=False)


def _refused(r):
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    code = detail.get("code") or detail.get("error")  # rules routes vs profile routes
    assert code == "RULES_HUMAN_ONLY", detail


def test_local_process_cannot_create_edit_revoke_or_preview_a_rule():
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test"), _card("p2", "npm run build")]
    with patch.dict(os.environ, _ENV), client:
        client.portal.call(_seed_sessions)
        lc = _local_client(client)
        with patch(_UIDS, return_value={33}):   # set-up write: nginx relaying the phone
            made = _always(lc, S1, "p1", "Bash(npm test:*)", "session",
                           headers={**_H, "X-Real-IP": _PHONE}).json()["rule"]["id"]
        with patch(_UIDS, return_value={_OPERATOR_UID}):
            _refused(_always(lc, S1, "p2", "Bash(npm run:*)", "global", confirm=True))
            _refused(lc.post(f"/api/sessions/{S1}/permissions/p2",
                             json={"decision": "always"}, headers=_H))
            _refused(lc.post("/api/permission-rules/preview", headers=_H, json={
                "sessionId": S1, "permId": "p2", "pattern": "Bash(npm:*)", "scope": "global"}))
            _refused(lc.patch(f"/api/permission-rules/{made}",
                              json={"pattern": "Bash(npm run:*)"}, headers=_H))
            _refused(lc.delete(f"/api/permission-rules/{made}", headers=_H))
            # Reads and plain answers are not rule writes.
            assert lc.get("/api/permission-rules", headers=_H).status_code == 200
        rules = client.portal.call(_rules)
        assert [(r["id"], r["pattern"]) for r in rules] == [(made, "Bash(npm test:*)")]
        assert [p["permId"] for p in fake.pending[S1]] == ["p2"]


def _socket_table(peer, server, _inode):
    """The edge's view of /proc for the bypass tests: the test client's own socket
    (127.0.0.1:50000) is a session's, port 40000 is tailscaled's live serve hop, and
    nothing else exists here."""
    return {50000: {_OPERATOR_UID}, 40000: {0}}.get(peer[1], set()) \
        if peer[0] == "127.0.0.1" else set()


@pytest.mark.parametrize("xff", [
    "100.64.0.9",         # pose as a remote tailnet device: no local socket to find
    "127.0.0.1:40000",    # pose as tailscaled's root-owned serve socket
])
def test_x_forwarded_for_cannot_launder_a_local_process_into_the_ui(xff):
    # The app applies X-Forwarded-For itself, so request.client becomes whatever the local
    # process wrote. The gate must judge the socket it actually connected from.
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    with patch.dict(os.environ, _ENV), client:
        client.portal.call(_seed_sessions)
        lc = _local_client(client)
        # Naming the operator's phone works for real: `tailscale whois` of that IP is the
        # operator, so identity resolves either way.
        whois = {"UserProfile": {"LoginName": "testuser@example.com"}}
        with patch(_UIDS, side_effect=_socket_table), \
                patch.object(identity, "_whois", return_value=whois):
            _refused(_always(lc, S1, "p1", "Bash(npm test:*)", "global", confirm=True,
                             headers={**_H, "X-Forwarded-For": xff}))
        assert client.portal.call(_rules) == []


_OPERATOR_WHOIS = {"UserProfile": {"LoginName": "testuser@example.com"}}
_PHONE_VIA_SERVE = ({0}, {"X-Forwarded-For": _PHONE})
_PHONE_VIA_NGINX = ({33}, {"X-Real-IP": _PHONE})


@pytest.mark.parametrize("uids,origin", [_PHONE_VIA_SERVE, _PHONE_VIA_NGINX])
def test_front_end_hop_for_a_remote_device_still_manages_rules(uids, origin):
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    with patch.dict(os.environ, _ENV), client:
        client.portal.call(_seed_sessions)
        lc = _local_client(client)
        h = {**_H, **origin}
        with patch(_UIDS, return_value=uids), \
                patch.object(identity, "_whois", return_value=_OPERATOR_WHOIS):
            r = _always(lc, S1, "p1", "Bash(npm test:*)", "session", headers=h)
            assert r.status_code == 200, r.text
            rid = r.json()["rule"]["id"]
            assert lc.patch(f"/api/permission-rules/{rid}", json={"pattern": "Bash(npm run:*)"},
                            headers=h).status_code == 200
            assert lc.delete(f"/api/permission-rules/{rid}", headers=h).status_code == 204


@pytest.mark.parametrize("uids,origin", [
    ({0}, {"X-Forwarded-For": _OWN}),     # curl https://<this node>:8443 from this node
    ({33}, {"X-Real-IP": _OWN}),          # curl https://<this node> (nginx)
    ({33}, {"X-Real-IP": "127.0.0.1"}),   # curl https://127.0.0.1 (nginx)
])
def test_local_process_going_through_a_front_end_is_still_local(uids, origin):
    client, fake = _make_client()
    fake.pending[S1] = [_card("p1", "npm test")]
    with patch.dict(os.environ, _ENV), client:
        client.portal.call(_seed_sessions)
        lc = _local_client(client)
        with patch(_UIDS, return_value=uids), \
                patch.object(identity, "_whois", return_value=_OPERATOR_WHOIS):
            _refused(_always(lc, S1, "p1", "Bash(npm test:*)", "global", confirm=True,
                             headers={**_H, **origin}))
        assert client.portal.call(_rules) == []


def test_local_process_cannot_write_permission_profile_patterns():
    # Launch-profile patterns are enforced like session rules (#2952), so writing them is
    # human-only. The patterns are ones the safety check accepts: only the gate refuses.
    client, _fake = _make_client()
    with patch.dict(os.environ, _ENV), client:
        lc = _local_client(client)
        with patch(_UIDS, return_value={33}):   # set-up write: nginx relaying the phone
            made = lc.post("/api/permission-profiles", headers={**_H, "X-Real-IP": _PHONE},
                           json={"name": "p", "patterns": ["Read"]}).json()["id"]
        with patch(_UIDS, return_value={_OPERATOR_UID}):
            _refused(lc.post("/api/permission-profiles", headers=_H,
                             json={"name": "q", "patterns": ["Bash(npm test:*)"]}))
            _refused(lc.patch(f"/api/permission-profiles/{made}", headers=_H,
                              json={"patterns": ["Bash(npm test:*)"]}))
            assert lc.get("/api/permission-profiles", headers=_H).status_code == 200
        with patch(_UIDS, return_value={33}):
            assert lc.patch(f"/api/permission-profiles/{made}",
                            headers={**_H, "X-Real-IP": _PHONE},
                            json={"patterns": ["Read", "Grep"]}).status_code == 200
        profiles = client.get("/api/permission-profiles", headers=_H).json()
    assert [(p["name"], p["patterns"]) for p in profiles] == [("p", ["Read", "Grep"])]


def test_ws_always_answer_goes_through_the_local_process_gate(ws_env):  # noqa: F811
    _app, orch, client = ws_env([])
    session_id = client.portal.call(orch.create_session, "test-ws-always-local").id
    seen: list[tuple[str, str | None]] = []

    async def _capture(*_a, decision, via, **_k):
        seen.append((decision, via))
        return {}

    with patch("server.bridge.ws.session_actions.resolve_permission", _capture), \
            patch("server.bridge.ws.human_gate_via", return_value=LOCAL_PROCESS_VIA):
        with client.websocket_connect("/ws", headers=_IDENTITY_HEADERS) as ws:
            ws.send_text(json.dumps({"type": "attach", "sessionId": session_id}))
            _drain_events(ws, until_type="replayComplete")
            for decision in ("always", "allow"):
                ws.send_text(json.dumps({"type": "permissionResponse", "sessionId": session_id,
                                         "permId": f"p-{decision}", "decision": decision}))
            deadline = time.monotonic() + 5.0
            while len(seen) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
    client.portal.call(orch.close_session, session_id)
    assert seen == [("always", LOCAL_PROCESS_VIA), ("allow", None)]
