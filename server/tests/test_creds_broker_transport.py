"""Edge → runner transport of Claude credential envelopes (#2629)."""
import asyncio

from server.ipc.creds_rpc import register_creds
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
    def __init__(self, ack=None, boom=False):
        self.ack, self.boom, self.calls = ack, boom, []

    async def creds_sync(self, envelopes, lease_s):
        if self.boom:
            raise RuntimeError("down")
        self.calls.append((envelopes, lease_s))
        return {"acks": self.ack if self.ack is not None
                else {e["account"]: e["generation"] for e in envelopes}}


def test_push_credentials_fans_out_to_runners_only(monkeypatch, caplog):
    env = ce.Envelope("a", "setup_token", "tok", None, 2)
    monkeypatch.setattr("server.sessions.claude_authority.mint_envelopes", lambda names: [env])
    monkeypatch.setattr("viberpit.accounts._anthropic_account_names", lambda: ["a"])
    ok, behind, dead, primary = _Runner(), _Runner(ack={"a": 1}), _Runner(boom=True), _Runner()
    fleet = FleetOrchestrator(_Router({"dobby": primary, "r1": ok, "r2": behind, "r3": dead}),
                              primary="dobby")

    async def _names():
        return ["dobby", "r1", "r2", "r3"]
    fleet._fanout_names = _names
    out = asyncio.run(fleet.push_credentials(lease_s=90))
    assert out == {"r1": {"a": 2}, "r2": {"a": 1}}
    assert primary.calls == []
    assert ok.calls[0][1] == 90 and ok.calls[0][0][0]["token"] == "tok"
    assert "behind on accounts=['a']" in caplog.text
