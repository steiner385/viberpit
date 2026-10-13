"""sync_cfg_mcp_oauth: the one dir <-> shared-store MCP OAuth sync (spec 2026-10-13).

Capture: a dir bundle that carries a refresh token is merged into the store (newer wins).
Publish: every store key reaches the dir as its public view (no refresh token)."""
import json

import pytest

from viberpit import creds, paths


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOME", str(tmp_path / "home"))


def _store_path(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "shared_mcp_path", lambda: tmp_path / "mcp.shared.json")
    return tmp_path / "mcp.shared.json"


def _setup(tmp_path, monkeypatch, shared, local, extra=None):
    tmp_path.mkdir(exist_ok=True)
    _store_path(tmp_path, monkeypatch).write_text(json.dumps(shared))
    cfg = tmp_path / "cfg"; cfg.mkdir()
    doc = {"mcpOAuth": local, **(extra or {})}
    (cfg / ".credentials.json").write_text(json.dumps(doc))
    return cfg


def _dir_mcp(cfg):
    return json.loads((cfg / ".credentials.json").read_text())["mcpOAuth"]


def _store(tmp_path):
    return json.loads((tmp_path / "mcp.shared.json").read_text())


# --- public_view --------------------------------------------------------------------------

def test_public_view_drops_only_the_refresh_token():
    bundle = {"serverName": "aws", "serverUrl": "https://s/mcp", "accessToken": "a", "refreshToken": "r",
              "expiresAt": 5, "scope": "x", "clientId": "c", "clientSecret": "s", "redirectUri": "u",
              "discoveryState": {"authorizationServerUrl": "https://as"}, "issuer": "https://as"}
    view = creds.public_view(bundle)
    assert "refreshToken" not in view
    assert view == {k: v for k, v in bundle.items() if k != "refreshToken"}
    assert bundle["refreshToken"] == "r"  # input untouched


def test_old_name_is_gone():
    assert not hasattr(creds, "refresh_cfg_mcp_oauth")


# --- publish (carried over from the post-login refresh) ------------------------------------

def test_sync_preserves_claude_oauth(tmp_path, monkeypatch):
    _store_path(tmp_path, monkeypatch).write_text(
        json.dumps({"notion|1": {"serverName": "notion", "accessToken": "new", "expiresAt": 2}}))
    cfg = tmp_path / "cfg"; cfg.mkdir()
    original = {"claudeAiOauth": {"accessToken": "live-rotated", "refreshToken": "r"}, "mcpOAuth": {}}
    (cfg / ".credentials.json").write_text(json.dumps(original))
    assert creds.sync_cfg_mcp_oauth(cfg) is True
    out = json.loads((cfg / ".credentials.json").read_text())
    assert out["claudeAiOauth"] == original["claudeAiOauth"]
    assert out["mcpOAuth"]["notion|1"]["accessToken"] == "new"


def test_sync_noop_when_up_to_date(tmp_path, monkeypatch):
    blob = {"k": {"serverName": "n", "accessToken": "t", "expiresAt": 5}}
    cfg = _setup(tmp_path, monkeypatch, blob, blob)
    assert creds.sync_cfg_mcp_oauth(cfg) is False


def test_sync_skips_missing_credentials_file(tmp_path, monkeypatch):
    _store_path(tmp_path, monkeypatch).write_text("{}")
    assert creds.sync_cfg_mcp_oauth(tmp_path) is False


def test_sync_keeps_newer_local_and_shares_a_new_key_without_refresh_token(tmp_path, monkeypatch):
    local = {"a": {"accessToken": "newer", "expiresAt": 9}, "b": {"accessToken": "x", "expiresAt": 1}}
    cfg = _setup(tmp_path, monkeypatch, {"a": {"accessToken": "old", "expiresAt": 1}}, local)
    assert creds.sync_cfg_mcp_oauth(cfg) is False
    assert _dir_mcp(cfg) == local
    # A key the store lacks is shared even without a refresh token (a no-RT provider login)...
    assert _store(tmp_path)["b"] == local["b"]
    # ...but an AT-only copy of a key the store has is never offered back.
    assert _store(tmp_path)["a"] == {"accessToken": "old", "expiresAt": 1}


def test_explicit_server_login_wins_without_expiry(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch,
                 {"k": {"serverName": "notion", "accessToken": "new"}},
                 {"k": {"serverName": "notion", "accessToken": "stale"}})
    assert creds.sync_cfg_mcp_oauth(cfg, server="notion") is True
    assert _dir_mcp(cfg)["k"]["accessToken"] == "new"


def test_non_explicit_server_does_not_override_equal_expiry(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch,
                 {"k": {"serverName": "notion", "accessToken": "new"}},
                 {"k": {"serverName": "notion", "accessToken": "stale"}})
    assert creds.sync_cfg_mcp_oauth(cfg, server="other") is False


def test_null_expiry_does_not_raise(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch,
                 {"k": {"serverName": "n", "accessToken": "a", "expiresAt": None}},
                 {"k": {"serverName": "n", "accessToken": "b", "expiresAt": None}})
    assert creds.sync_cfg_mcp_oauth(cfg) is False
    cfg2 = _setup(tmp_path / "two", monkeypatch,
                  {"k": {"serverName": "n", "accessToken": "a", "expiresAt": 5}},
                  {"k": {"serverName": "n", "accessToken": "b", "expiresAt": None}})
    assert creds.sync_cfg_mcp_oauth(cfg2) is True


def test_skips_write_when_file_replaced_between_read_and_write(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch,
                 {"k": {"serverName": "n", "accessToken": "new", "expiresAt": 9}}, {},
                 {"claudeAiOauth": {"accessToken": "old"}})
    cred = cfg / ".credentials.json"
    real = creds.read_json
    newer = {"claudeAiOauth": {"accessToken": "rotated"}, "mcpOAuth": {}}

    def racing(p):
        out = real(p)
        if str(p).endswith(".credentials.json"):
            tmp = cfg / "x.tmp"
            tmp.write_text(json.dumps(newer))
            tmp.replace(cred)  # new inode, like the CLI's atomic rotate
        return out

    monkeypatch.setattr(creds, "read_json", racing)
    assert creds.sync_cfg_mcp_oauth(cfg) is False
    assert json.loads(cred.read_text()) == newer


def test_plugin_prefixed_server_name_counts_as_explicit(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch,
                 {"k": {"serverName": "plugin:p:notion", "accessToken": "new"}},
                 {"k": {"serverName": "plugin:p:notion", "accessToken": "stale"}})
    assert creds.sync_cfg_mcp_oauth(cfg, server="notion") is True
    assert _dir_mcp(cfg)["k"]["accessToken"] == "new"


# --- capture + strip ------------------------------------------------------------------------

def test_publish_strips_refresh_token_from_dir(tmp_path, monkeypatch):
    bundle = {"serverName": "aws", "accessToken": "a", "refreshToken": "r", "expiresAt": 5, "clientId": "c"}
    cfg = _setup(tmp_path, monkeypatch, {"k": bundle}, {"k": bundle})
    assert creds.sync_cfg_mcp_oauth(cfg) is True
    assert _dir_mcp(cfg)["k"] == creds.public_view(bundle)
    assert _store(tmp_path)["k"] == bundle  # the store keeps the only refresh token


def test_publish_never_writes_a_refresh_token_into_the_dir(tmp_path, monkeypatch):
    bundle = {"serverName": "aws", "accessToken": "a", "refreshToken": "r", "expiresAt": 5}
    cfg = _setup(tmp_path, monkeypatch, {"k": bundle}, {})
    assert creds.sync_cfg_mcp_oauth(cfg) is True
    assert "refreshToken" not in _dir_mcp(cfg)["k"]


def test_capture_keeps_a_dir_only_rotation_with_later_expiry(tmp_path, monkeypatch):
    old = {"serverName": "aws", "accessToken": "a1", "refreshToken": "r1", "expiresAt": 5}
    rotated = {"serverName": "aws", "accessToken": "a2", "refreshToken": "r2", "expiresAt": 9}
    cfg = _setup(tmp_path, monkeypatch, {"k": old}, {"k": rotated}, {"claudeAiOauth": {"accessToken": "c"}})
    assert creds.sync_cfg_mcp_oauth(cfg) is True
    assert _store(tmp_path)["k"] == rotated
    assert _dir_mcp(cfg)["k"] == creds.public_view(rotated)
    assert json.loads((cfg / ".credentials.json").read_text())["claudeAiOauth"] == {"accessToken": "c"}


def test_capture_does_not_roll_back_a_newer_store_bundle(tmp_path, monkeypatch):
    newer = {"serverName": "aws", "accessToken": "a2", "refreshToken": "r2", "expiresAt": 9}
    stale = {"serverName": "aws", "accessToken": "a1", "refreshToken": "r1", "expiresAt": 5}
    cfg = _setup(tmp_path, monkeypatch, {"k": newer}, {"k": stale})
    assert creds.sync_cfg_mcp_oauth(cfg) is True
    assert _store(tmp_path)["k"] == newer
    assert _dir_mcp(cfg)["k"] == creds.public_view(newer)


def test_capture_of_a_dir_only_key_with_refresh_token(tmp_path, monkeypatch):
    fresh = {"serverName": "linear", "accessToken": "a", "refreshToken": "r", "expiresAt": 7}
    cfg = _setup(tmp_path, monkeypatch, {}, {"lin": fresh})
    assert creds.sync_cfg_mcp_oauth(cfg) is True
    assert _store(tmp_path) == {"lin": fresh}
    assert _dir_mcp(cfg) == {"lin": creds.public_view(fresh)}


def test_cleared_dir_and_cleared_store_stay_cleared(tmp_path, monkeypatch):
    cleared = {"serverName": "aws", "accessToken": "", "expiresAt": 0, "clientId": "c"}
    cfg = _setup(tmp_path, monkeypatch, {"k": cleared}, {"k": cleared})
    assert creds.sync_cfg_mcp_oauth(cfg) is False
    assert _dir_mcp(cfg)["k"] == cleared
    assert _store(tmp_path)["k"] == cleared


def test_cleared_dir_is_never_captured(tmp_path, monkeypatch):
    live = {"serverName": "aws", "accessToken": "a", "refreshToken": "r", "expiresAt": 9}
    cleared = {"serverName": "aws", "accessToken": "", "expiresAt": 0}
    cfg = _setup(tmp_path, monkeypatch, {"k": live}, {"k": cleared})
    creds.sync_cfg_mcp_oauth(cfg)
    assert _store(tmp_path)["k"] == live


# --- the locked update helper ---------------------------------------------------------------

def test_update_shared_mcp_writes_only_on_change(tmp_path, monkeypatch):
    path = _store_path(tmp_path, monkeypatch)
    assert creds.update_shared_mcp(lambda store: store) == {}
    assert not path.exists()
    out = creds.update_shared_mcp(lambda store: {**store, "k": {"accessToken": "a"}})
    assert out == {"k": {"accessToken": "a"}} == _store(tmp_path)


def test_one_short_name_helper():
    from viberpit.mcp_names import short_name
    from server import mcp_auth
    from server.mcp_health import store
    assert short_name("plugin:railway:railway") == "railway" and short_name("notion") == "notion"
    assert store.short_name is short_name
    assert not hasattr(mcp_auth, "_bare")
