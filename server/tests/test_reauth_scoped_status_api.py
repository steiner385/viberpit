"""GET /api/accounts/{name}/reauth is scoped to the account (Bug 1 regression).

A reauth that completed `success` for one account must read as `idle` when a
*different* account polls its status — otherwise the leaked terminal state
freezes reauth on every other account (the frontend stops polling on a terminal
state). Built as a minimal app so it needs no claude-agent-sdk.
"""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.api.accounts import router
from server.auth.middleware import current_identity
from server.reauth import ReauthManager, ReauthState
from server.reauth_remote import InProcessReauthAdapter
from ccswitch import paths


def _make_client(tmp_home, monkeypatch, *, state: ReauthState):
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_home))
    # Two real credential slots so _check_name_and_slot passes for both accounts.
    for name in ("acctA", "acctB"):
        paths.slot_path(name).parent.mkdir(parents=True, exist_ok=True)
        paths.slot_path(name).write_text(json.dumps({"accessToken": "x"}))

    mgr = ReauthManager(key_available=lambda: True)
    mgr._state = state

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[current_identity] = lambda: "tester"
    # Production wraps the manager in the async adapter — exercise the real chain.
    app.state.reauth = InProcessReauthAdapter(mgr)
    return TestClient(app)


def test_other_account_reads_idle_when_one_account_succeeded(tmp_path, monkeypatch):
    success_for_A = ReauthState(state="success", account="acctA", identity="a@example.com")
    client = _make_client(tmp_path, monkeypatch, state=success_for_A)

    # The owning account still sees its terminal success.
    assert client.get("/api/accounts/acctA/reauth").json()["state"] == "success"
    # A different account must NOT inherit it — it reads idle.
    assert client.get("/api/accounts/acctB/reauth").json()["state"] == "idle"
