"""Runner side of MCP distribution (#3287): the ``creds_sync`` handler caches the pushed MCP
public view, publishes it into provably-local account dirs and offers terminal logins back.

The handler is built the way ``test_creds_broker_transport`` builds it (a bare ``_App`` with
``state.rpc``); the mount table is injected through ``runner_mcp.is_shared_dir`` so the real
mount logic runs against a fleet this test is not on."""
import asyncio
import json
import os

import pytest

from server.ipc import runner_mcp
from server.ipc.creds_rpc import register_creds
from server.obs import logdir
from viberpit import creds
from viberpit import credential_envelope as ce
from viberpit.configdir import CREDENTIALS_NAME
from viberpit.mcp_view_store import reset_runner_view, runner_view

LOCAL = [("/", "ext4")]

CLAUDE = {"accessToken": "claude-at", "refreshToken": "claude-rt", "expiresAt": 9e12}


class _App:
    class state:
        rpc: dict = {}


def _wire(account="a", gen=1, token="tok"):
    return {"account": account, "kind": "setup_token", "token": token,
            "expires_at": None, "generation": gen}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A worker whose account tree and host-local state live under *tmp_path*."""
    home = tmp_path / "home"
    monkeypatch.setenv("VIBERPIT_HOME", str(home))
    monkeypatch.setenv("VIBERPIT_CLAUDE_CREDS_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VIBERPIT_DAEMON_ROLE", "worker")
    reset_runner_view()
    ce.reset_runner_store()
    yield home
    reset_runner_view()
    ce.reset_runner_store()


def _shared_table(home):
    return [("/", "ext4"), (os.path.realpath(home), "fuse.sshfs")]


def _mounts(monkeypatch, table):
    """Route the module's mount check through the real logic against *table*."""
    monkeypatch.setattr(runner_mcp, "is_shared_dir",
                        lambda path, *, mounts=None: logdir.is_shared_dir(path, mounts=table))


def _account(home, name, mcp):
    path = home / "cfg" / name / CREDENTIALS_NAME
    creds.write_json_secure(path, {"claudeAiOauth": dict(CLAUDE), "mcpOAuth": mcp})
    return path


def _mcp(path):
    return json.loads(path.read_text()).get("mcpOAuth")


def _app():
    app = _App()
    app.state.rpc = {}
    register_creds(app)
    return app


def _sync(app, **extra):
    return asyncio.run(app.state.rpc["creds_sync"]({"envelopes": [_wire()], "lease_s": 60, **extra}))


def test_handler_without_mcp_fields_is_envelopes_only(home, monkeypatch):
    _mounts(monkeypatch, LOCAL)
    path = _account(home, "acct", {"k": {"accessToken": "d", "refreshToken": "rt"}})
    before = path.read_bytes()
    assert _sync(_app()) == {"acks": {"a": 1}}
    assert path.read_bytes() == before
    assert runner_view().bundles() is None


def test_view_is_published_into_local_dirs(home, monkeypatch):
    _mounts(monkeypatch, LOCAL)
    one = _account(home, "one", {})
    two = _account(home, "two", {"k": {"accessToken": "older", "expiresAt": 1}})
    bundle = {"serverName": "aws", "accessToken": "at", "expiresAt": 9e12}
    res = _sync(_app(), mcp_bundles={"k": bundle}, mcp_drop=[])
    assert res == {"acks": {"a": 1}}  # a refresh-less copy of a key the view holds is not an offer
    assert _mcp(one) == {"k": bundle}
    assert _mcp(two) == {"k": bundle}
    for path in (one, two):
        assert json.loads(path.read_text())["claudeAiOauth"] == CLAUDE
    assert runner_view().bundles() == {"k": bundle}


def test_drop_keys_are_purged_from_local_dirs(home, monkeypatch):
    _mounts(monkeypatch, LOCAL)
    path = _account(home, "acct", {"old": {"accessToken": "stale"}})
    res = _sync(_app(), mcp_bundles={}, mcp_drop=["old"])
    assert "mcp_offers" not in res
    assert _mcp(path) == {}
    assert runner_view().drop_keys() == frozenset({"old"})


def test_local_login_is_offered_then_stripped(home, monkeypatch):
    _mounts(monkeypatch, LOCAL)
    login = {"accessToken": "d", "refreshToken": "MCP-REFRESH-SECRET", "expiresAt": 9e12}
    path = _account(home, "acct", {"k": login})
    app = _app()
    # the view lacks "k": the terminal login is offered and stays in the dir
    res = _sync(app, mcp_bundles={}, mcp_drop=[])
    assert res["mcp_offers"] == {"k": login}
    assert _mcp(path) == {"k": login}
    # the edge kept an OLDER bundle for that key: still offered (read before the publish),
    # but the publish replaces the refresh-bearing dir copy with the public view
    edge = {"accessToken": "edge", "expiresAt": 1}
    res = _sync(app, mcp_bundles={"k": edge}, mcp_drop=[])
    assert res["mcp_offers"] == {"k": login}
    assert _mcp(path) == {"k": edge}
    assert "MCP-REFRESH-SECRET" not in path.read_text()
    # nothing left to offer once the dir holds the public view
    res = _sync(app, mcp_bundles={"k": edge}, mcp_drop=[])
    assert "mcp_offers" not in res


def test_shared_mount_tree_is_never_written(home, monkeypatch):
    _mounts(monkeypatch, _shared_table(home))
    path = _account(home, "acct", {"k": {"accessToken": "d", "refreshToken": "rt"}})
    before = path.read_bytes()
    view = {"k": {"accessToken": "edge", "expiresAt": 9e12}, "n": {"accessToken": "n"}}
    res = _sync(_app(), mcp_bundles=view, mcp_drop=["gone"])
    assert res == {"acks": {"a": 1}}
    assert runner_view().bundles() == view
    assert runner_view().drop_keys() == frozenset({"gone"})
    assert path.read_bytes() == before


def test_primary_never_publishes_from_a_push(home, monkeypatch):
    monkeypatch.delenv("VIBERPIT_DAEMON_ROLE", raising=False)
    path = _account(home, "acct", {})
    before = path.read_bytes()
    runner_view().sync({"k": {"accessToken": "at"}})
    assert runner_mcp.owns_account_dirs(mounts=LOCAL) is False
    assert runner_mcp.sync_local_dirs(mounts=LOCAL) == {}
    assert path.read_bytes() == before


def test_owns_account_dirs_fails_closed_on_unknown_mounts(home):
    assert runner_mcp.owns_account_dirs(mounts=[]) is False
    assert runner_mcp.owns_account_dirs(mounts=_shared_table(home)) is False
    assert runner_mcp.owns_account_dirs(mounts=LOCAL) is True


def test_sync_local_dirs_without_a_view_touches_nothing(home):
    path = _account(home, "acct", {"k": {"accessToken": "d", "refreshToken": "rt"}})
    before = path.read_bytes()
    assert runner_mcp.sync_local_dirs(mounts=LOCAL) == {}
    assert path.read_bytes() == before


def test_a_torn_dir_is_contained(home, monkeypatch, caplog):
    _mounts(monkeypatch, LOCAL)
    good = _account(home, "good", {})
    torn = home / "cfg" / "torn" / CREDENTIALS_NAME
    torn.parent.mkdir(parents=True)
    torn.write_text("{not json")
    bundle = {"accessToken": "MCP-ACCESS-SECRET", "expiresAt": 9e12}
    res = _sync(_app(), mcp_bundles={"k": bundle}, mcp_drop=[])
    assert res == {"acks": {"a": 1}}
    assert _mcp(good) == {"k": bundle}
    assert torn.read_text() == "{not json"
    assert "MCP-ACCESS-SECRET" not in caplog.text
