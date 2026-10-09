import json

from viberpit import creds, paths


def test_refresh_preserves_claude_oauth(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "shared_mcp_path", lambda: tmp_path / "mcp.shared.json")
    (tmp_path / "mcp.shared.json").write_text(json.dumps({"notion|1": {"serverName": "notion", "accessToken": "new", "expiresAt": 2}}))
    cfg = tmp_path / "cfg"; cfg.mkdir()
    original = {"claudeAiOauth": {"accessToken": "live-rotated", "refreshToken": "r"}, "mcpOAuth": {}}
    (cfg / ".credentials.json").write_text(json.dumps(original))
    assert creds.refresh_cfg_mcp_oauth(cfg) is True
    out = json.loads((cfg / ".credentials.json").read_text())
    assert out["claudeAiOauth"] == original["claudeAiOauth"]
    assert out["mcpOAuth"]["notion|1"]["accessToken"] == "new"


def test_refresh_noop_when_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "shared_mcp_path", lambda: tmp_path / "mcp.shared.json")
    blob = {"k": {"serverName": "n", "accessToken": "t", "expiresAt": 5}}
    (tmp_path / "mcp.shared.json").write_text(json.dumps(blob))
    cfg = tmp_path / "cfg"; cfg.mkdir()
    (cfg / ".credentials.json").write_text(json.dumps({"mcpOAuth": blob}))
    assert creds.refresh_cfg_mcp_oauth(cfg) is False


def test_refresh_skips_missing_credentials_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "shared_mcp_path", lambda: tmp_path / "mcp.shared.json")
    (tmp_path / "mcp.shared.json").write_text("{}")
    assert creds.refresh_cfg_mcp_oauth(tmp_path) is False


def test_refresh_keeps_newer_local_and_local_only_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "shared_mcp_path", lambda: tmp_path / "mcp.shared.json")
    (tmp_path / "mcp.shared.json").write_text(json.dumps({"a": {"accessToken": "old", "expiresAt": 1}}))
    cfg = tmp_path / "cfg"; cfg.mkdir()
    local = {"a": {"accessToken": "newer", "expiresAt": 9}, "b": {"accessToken": "x", "expiresAt": 1}}
    (cfg / ".credentials.json").write_text(json.dumps({"mcpOAuth": local}))
    assert creds.refresh_cfg_mcp_oauth(cfg) is False
    assert json.loads((cfg / ".credentials.json").read_text())["mcpOAuth"] == local


def _setup(tmp_path, monkeypatch, shared, local, extra=None):
    tmp_path.mkdir(exist_ok=True)
    monkeypatch.setattr(paths, "shared_mcp_path", lambda: tmp_path / "mcp.shared.json")
    (tmp_path / "mcp.shared.json").write_text(json.dumps(shared))
    cfg = tmp_path / "cfg"; cfg.mkdir()
    doc = {"mcpOAuth": local, **(extra or {})}
    (cfg / ".credentials.json").write_text(json.dumps(doc))
    return cfg


def test_explicit_server_login_wins_without_expiry(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch,
                 {"k": {"serverName": "notion", "accessToken": "new"}},
                 {"k": {"serverName": "notion", "accessToken": "stale"}})
    assert creds.refresh_cfg_mcp_oauth(cfg, server="notion") is True
    assert json.loads((cfg / ".credentials.json").read_text())["mcpOAuth"]["k"]["accessToken"] == "new"


def test_non_explicit_server_does_not_override_equal_expiry(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch,
                 {"k": {"serverName": "notion", "accessToken": "new"}},
                 {"k": {"serverName": "notion", "accessToken": "stale"}})
    assert creds.refresh_cfg_mcp_oauth(cfg, server="other") is False


def test_null_expiry_does_not_raise(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch,
                 {"k": {"serverName": "n", "accessToken": "a", "expiresAt": None}},
                 {"k": {"serverName": "n", "accessToken": "b", "expiresAt": None}})
    assert creds.refresh_cfg_mcp_oauth(cfg) is False
    cfg2 = _setup(tmp_path / "two", monkeypatch,
                  {"k": {"serverName": "n", "accessToken": "a", "expiresAt": 5}},
                  {"k": {"serverName": "n", "accessToken": "b", "expiresAt": None}})
    assert creds.refresh_cfg_mcp_oauth(cfg2) is True


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
    assert creds.refresh_cfg_mcp_oauth(cfg) is False
    assert json.loads(cred.read_text()) == newer


def test_plugin_prefixed_server_name_counts_as_explicit(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch,
                 {"k": {"serverName": "plugin:p:notion", "accessToken": "new"}},
                 {"k": {"serverName": "plugin:p:notion", "accessToken": "stale"}})
    assert creds.refresh_cfg_mcp_oauth(cfg, server="notion") is True
    assert json.loads((cfg / ".credentials.json").read_text())["mcpOAuth"]["k"]["accessToken"] == "new"


def test_one_short_name_helper():
    from viberpit.mcp_names import short_name
    from server import mcp_auth
    from server.mcp_health import store
    assert short_name("plugin:railway:railway") == "railway" and short_name("notion") == "notion"
    assert store.short_name is short_name
    assert not hasattr(mcp_auth, "_bare")
