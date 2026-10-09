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
