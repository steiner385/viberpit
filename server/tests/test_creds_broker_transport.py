"""Edge → runner transport of Claude credential envelopes (#2629) and, riding the same push,
the MCP public view down and runner offers back (#3287)."""
import asyncio
import json
import logging

import pytest

from server.ipc.creds_rpc import register_creds
from server.sessions import fleet_orchestrator as fo
from server.sessions.fleet_orchestrator import FleetOrchestrator
from viberpit import credential_envelope as ce


class _App:
    class state:
        rpc: dict = {}


def _wire(account="a", gen=1, token="tok"):
    return {"account": account, "kind": "setup_token", "token": token,
            "expires_at": None, "generation": gen}


def test_creds_sync_rpc_stores_and_acks(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBERPIT_CLAUDE_CREDS_DIR", str(tmp_path))
    ce.reset_runner_store()
    app = _App()
    app.state.rpc = {}
    register_creds(app)
    res = asyncio.run(app.state.rpc["creds_sync"]({"envelopes": [_wire(gen=3)], "lease_s": 60}))
    assert res == {"acks": {"a": 3}}
    assert ce.runner_store().launch_token("a") == "tok"
    # a superseded rotation is refused: the ack still reports the higher generation
    res = asyncio.run(app.state.rpc["creds_sync"]({"envelopes": [_wire(gen=2, token="old")],
                                                   "lease_s": 60}))
    assert res == {"acks": {"a": 3}}
    assert ce.runner_store().launch_token("a") == "tok"
    ce.reset_runner_store()


class _Router:
    def __init__(self, hosts):
        self._h = hosts

    def get(self, name):
        return self._h[name]

    def names(self):
        return list(self._h)


class _Runner:
    """Records each push as ``(envelopes, lease_s, kw)``; *offers* rides back as ``mcp_offers``
    (an old runner — ``offers=None`` — answers with acks only)."""

    def __init__(self, ack=None, boom=False, offers=None):
        self.ack, self.boom, self.offers, self.calls = ack, boom, offers, []

    async def creds_sync(self, envelopes, lease_s, **kw):
        if self.boom:
            raise RuntimeError("down")
        self.calls.append((envelopes, lease_s, kw))
        res = {"acks": self.ack if self.ack is not None
               else {e["account"]: e["generation"] for e in envelopes}}
        if self.offers is not None:
            res["mcp_offers"] = self.offers
        return res


def _mint_one(monkeypatch, rc=()):
    env = ce.Envelope("a", "setup_token", "tok", None, 2)
    monkeypatch.setattr("server.sessions.claude_authority.mint_envelopes", lambda names: [env])
    monkeypatch.setattr("server.sessions.claude_authority.mint_remote_control_envelopes",
                        lambda names: list(rc))
    monkeypatch.setattr("viberpit.accounts._anthropic_account_names", lambda: ["a"])


def _fleet(hosts):
    fleet = FleetOrchestrator(_Router(hosts), primary="dobby")

    async def _names():
        return list(hosts)
    fleet._fanout_names = _names
    return fleet


def test_push_credentials_fans_out_to_runners_only(monkeypatch, caplog):
    _mint_one(monkeypatch)
    ok, behind, dead, primary = _Runner(), _Runner(ack={"a": 1}), _Runner(boom=True), _Runner()
    fleet = _fleet({"dobby": primary, "r1": ok, "r2": behind, "r3": dead})
    out = asyncio.run(fleet.push_credentials(lease_s=90))
    assert out == {"r1": {"a": 2}, "r2": {"a": 1}}
    assert primary.calls == []
    assert ok.calls[0][1] == 90 and ok.calls[0][0][0]["token"] == "tok"
    assert "behind on accounts=['a']" in caplog.text


# --- the MCP public view rides the push; runner offers ride back (#3287) ------------------

STORE = {"k": {"accessToken": "a", "refreshToken": "RT-secret", "expiresAt": 1}, "junk": 3}


def test_push_carries_mcp_public_view_and_drop(monkeypatch):
    _mint_one(monkeypatch)
    monkeypatch.setattr("viberpit.creds.read_shared_mcp", lambda: STORE)
    monkeypatch.setattr("server.mcp_health.grants.purge_keys",
                        lambda store, **kw: frozenset({"gone"}) if store is STORE else pytest.fail(
                            "purge_keys must see the store the tick read"))
    ok = _Runner()
    asyncio.run(_fleet({"dobby": _Runner(), "r1": ok}).push_credentials())
    kw = ok.calls[0][2]
    assert kw == {"mcp_bundles": {"k": {"accessToken": "a", "expiresAt": 1}}, "mcp_drop": ["gone"]}
    assert "RT-secret" not in json.dumps(kw)


def test_push_merges_runner_offers(monkeypatch, caplog):
    _mint_one(monkeypatch)
    monkeypatch.setattr("viberpit.creds.read_shared_mcp", dict)
    offers = {"k2": {"accessToken": "x", "refreshToken": "y-secret"}}
    merged: list = []
    monkeypatch.setattr("viberpit.creds.merge_shared_mcp",
                        lambda incoming, **kw: merged.append((incoming, kw)) or 1)
    with caplog.at_level(logging.INFO, logger=fo.log.name):
        out = asyncio.run(_fleet({"dobby": _Runner(), "r1": _Runner(offers=offers)}).push_credentials())
    assert out == {"r1": {"a": 2}}
    assert merged == [(offers, {})]
    assert "fleet.creds_sync host=r1 offered mcp bundle(s) keys=['k2']" in caplog.text
    assert "y-secret" not in caplog.text


def test_push_tolerates_runner_without_offers(monkeypatch):
    _mint_one(monkeypatch)
    monkeypatch.setattr("viberpit.creds.read_shared_mcp", dict)
    monkeypatch.setattr("viberpit.creds.merge_shared_mcp",
                        lambda incoming, **kw: pytest.fail("nothing to merge"))
    old = _Runner()                    # an old runner: acks only, no mcp_offers
    empty = _Runner(offers={})         # a new runner with nothing to offer
    out = asyncio.run(_fleet({"dobby": _Runner(), "r1": old, "r2": empty}).push_credentials())
    assert out == {"r1": {"a": 2}, "r2": {"a": 2}}


def test_push_merge_failure_is_a_warning_and_keeps_the_acks(monkeypatch, caplog):
    _mint_one(monkeypatch)
    monkeypatch.setattr("viberpit.creds.read_shared_mcp", dict)

    def boom(incoming, **kw):
        raise OSError("store locked")
    monkeypatch.setattr("viberpit.creds.merge_shared_mcp", boom)
    runner = _Runner(offers={"k2": {"accessToken": "x", "refreshToken": "y-secret"}})
    with caplog.at_level(logging.INFO, logger=fo.log.name):
        out = asyncio.run(_fleet({"dobby": _Runner(), "r1": runner}).push_credentials())
    assert out == {"r1": {"a": 2}}
    [rec] = [r for r in caplog.records if "merge" in r.getMessage()]
    assert rec.levelno == logging.WARNING and "host=r1" in rec.getMessage()
    assert "y-secret" not in caplog.text


def test_push_without_a_readable_store_still_pushes_envelopes(monkeypatch, caplog):
    """A broken MCP store must not cost the runners their Claude envelope lease: the push
    goes out without the MCP fields (so the runner keeps its cached view) and warns."""
    _mint_one(monkeypatch)

    def boom():
        raise OSError("store unreadable")
    monkeypatch.setattr("viberpit.creds.read_shared_mcp", boom)
    ok = _Runner()
    with caplog.at_level(logging.INFO, logger=fo.log.name):
        out = asyncio.run(_fleet({"dobby": _Runner(), "r1": ok}).push_credentials())
    assert out == {"r1": {"a": 2}}
    assert ok.calls[0][2] == {}
    assert any(r.levelno == logging.WARNING and "mcp view" in r.getMessage() for r in caplog.records)


# --- Remote Control envelopes ride the same push in their own field -------------------------

RC_ENV = ce.Envelope("a", ce.OAUTH_ACCESS, "rc-AT-secret", 9e9, 4, org_uuid="org-1")


def _rc_wire(gen=4, token="rc-AT-secret", **over):
    return {"account": "a", "kind": "oauth_access", "token": token, "expires_at": 9e9,
            "generation": gen, "org_uuid": "org-1", **over}


def test_push_carries_rc_envelopes_beside_the_inference_ones(monkeypatch):
    _mint_one(monkeypatch, rc=[RC_ENV])
    monkeypatch.setattr("viberpit.creds.read_shared_mcp", dict)
    ok = _Runner()
    asyncio.run(_fleet({"dobby": _Runner(), "r1": ok}).push_credentials())
    envelopes, _lease, kw = ok.calls[0]
    assert [e["kind"] for e in envelopes] == ["setup_token"]          # inference unchanged
    assert kw["rc_envelopes"] == [_rc_wire()]
    assert "refresh" not in json.dumps(kw).lower()


def test_push_without_rc_envelopes_leaves_the_wire_as_before(monkeypatch):
    _mint_one(monkeypatch)
    monkeypatch.setattr("viberpit.creds.read_shared_mcp", dict)
    ok = _Runner()
    asyncio.run(_fleet({"dobby": _Runner(), "r1": ok}).push_credentials())
    assert "rc_envelopes" not in ok.calls[0][2]


def test_a_failed_rc_mint_never_costs_the_inference_lease(monkeypatch, caplog):
    _mint_one(monkeypatch)
    monkeypatch.setattr("viberpit.creds.read_shared_mcp", dict)

    def boom(names):
        raise OSError("slot unreadable")
    monkeypatch.setattr("server.sessions.claude_authority.mint_remote_control_envelopes", boom)
    ok = _Runner()
    with caplog.at_level(logging.INFO, logger=fo.log.name):
        out = asyncio.run(_fleet({"dobby": _Runner(), "r1": ok}).push_credentials())
    assert out == {"r1": {"a": 2}} and "rc_envelopes" not in ok.calls[0][2]
    assert any(r.levelno == logging.WARNING and "remote control" in r.getMessage()
               for r in caplog.records)


def test_a_runner_behind_on_rc_generations_is_a_warning(monkeypatch, caplog):
    _mint_one(monkeypatch, rc=[RC_ENV])
    monkeypatch.setattr("viberpit.creds.read_shared_mcp", dict)

    class _RcRunner(_Runner):
        async def creds_sync(self, envelopes, lease_s, **kw):
            res = await super().creds_sync(envelopes, lease_s, **kw)
            res["rc_acks"] = {"a": 3}
            return res
    with caplog.at_level(logging.INFO, logger=fo.log.name):
        asyncio.run(_fleet({"dobby": _Runner(), "r1": _RcRunner()}).push_credentials())
    assert "behind on remote control accounts=['a']" in caplog.text
    assert "rc-AT-secret" not in caplog.text


def _rpc(tmp_path, monkeypatch, orchestrator=None):
    monkeypatch.setenv("VIBERPIT_CLAUDE_CREDS_DIR", str(tmp_path))
    ce.reset_runner_store()
    app = _App()
    app.state.rpc = {}
    app.state.orchestrator = orchestrator    # class-level state: always (re)set
    register_creds(app)
    return app.state.rpc["creds_sync"]


def test_creds_sync_rpc_stores_rc_envelopes_and_acks_them(tmp_path, monkeypatch):
    sync = _rpc(tmp_path, monkeypatch)
    try:
        res = asyncio.run(sync({"envelopes": [_wire(gen=3)], "lease_s": 60,
                                "rc_envelopes": [_rc_wire()]}))
        assert res == {"acks": {"a": 3}, "rc_acks": {"a": 4}}
        held = ce.rc_runner_store().launch_envelope("a")
        assert (held.token, held.org_uuid) == ("rc-AT-secret", "org-1")
        assert ce.runner_store().launch_token("a") == "tok"           # inference untouched
        # A superseded rotation is refused; the ack still names the higher generation.
        res = asyncio.run(sync({"envelopes": [_wire(gen=3)], "lease_s": 60,
                                "rc_envelopes": [_rc_wire(gen=2, token="old")]}))
        assert res["rc_acks"] == {"a": 4}
        # An edge that vends none (or an old edge, which never sends the field): dropped.
        res = asyncio.run(sync({"envelopes": [_wire(gen=3)], "lease_s": 60}))
        assert res == {"acks": {"a": 3}}
        assert ce.rc_runner_store().launch_envelope("a") is None
    finally:
        ce.reset_runner_store()


def test_malformed_rc_envelopes_are_skipped_never_fail_the_sync(tmp_path, monkeypatch):
    sync = _rpc(tmp_path, monkeypatch)
    try:
        bad = [{"account": "a"}, _rc_wire(kind="setup_token"), _rc_wire(org_uuid=None), "x"]
        res = asyncio.run(sync({"envelopes": [_wire(gen=3)], "lease_s": 60,
                                "rc_envelopes": bad}))
        assert res == {"acks": {"a": 3}}
        assert ce.rc_runner_store().launch_envelope("a") is None
    finally:
        ce.reset_runner_store()


def test_creds_sync_tells_the_orchestrator(tmp_path, monkeypatch):
    calls = []

    class _Orch:
        def remote_control_credentials_synced(self):
            calls.append(1)
    sync = _rpc(tmp_path, monkeypatch, orchestrator=_Orch())
    try:
        asyncio.run(sync({"envelopes": [], "lease_s": 60, "rc_envelopes": [_rc_wire()]}))
        assert calls == [1]
    finally:
        ce.reset_runner_store()
