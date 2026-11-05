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


def _mint_one(monkeypatch):
    env = ce.Envelope("a", "setup_token", "tok", None, 2)
    monkeypatch.setattr("server.sessions.claude_authority.mint_envelopes", lambda names: [env])
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
