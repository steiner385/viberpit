import json
import pytest
from server import mcp_auth

@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    cfg_root = tmp_path / "cfg"; cfg_root.mkdir()
    shared = tmp_path / "store" / "mcp.shared.json"
    monkeypatch.setattr(mcp_auth.paths, "cfg_root", lambda: cfg_root)
    monkeypatch.setattr(mcp_auth.paths, "shared_mcp_path", lambda: shared)
    return cfg_root, shared

def test_merge_adds_new_server_keys(fake_root):
    cfg_root, shared = fake_root
    acct = cfg_root / "alice"; acct.mkdir()
    (acct / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"x": 1}, "mcpOAuth": {"notion": {"tok": "A"}}}))
    n = mcp_auth.merge_mcp_oauth_from_cfg(acct)
    assert n == 1
    assert json.loads(shared.read_text())["notion"] == {"tok": "A"}

def test_merge_preserves_other_servers(fake_root):
    cfg_root, shared = fake_root
    shared.parent.mkdir(parents=True)
    shared.write_text(json.dumps({"linear": {"tok": "L"}}))
    acct = cfg_root / "alice"; acct.mkdir()
    (acct / ".credentials.json").write_text(json.dumps({"mcpOAuth": {"notion": {"tok": "A"}}}))
    mcp_auth.merge_mcp_oauth_from_cfg(acct)
    data = json.loads(shared.read_text())
    assert set(data) == {"linear", "notion"}

def test_merge_missing_file_is_noop(fake_root):
    cfg_root, _ = fake_root
    acct = cfg_root / "empty"; acct.mkdir()
    assert mcp_auth.merge_mcp_oauth_from_cfg(acct) == 0

def test_clear_removes_bare_and_plugin_keys_across_accounts(fake_root):
    cfg_root, _ = fake_root
    a = cfg_root / "alice"; a.mkdir()
    b = cfg_root / "bob"; b.mkdir()
    (a / "mcp-needs-auth-cache.json").write_text(json.dumps({"notion": {"t": 1}, "linear": {"t": 2}}))
    (b / "mcp-needs-auth-cache.json").write_text(json.dumps({"plugin:notion:notion": {"t": 3}}))
    cleared = mcp_auth.clear_needs_auth_cache("notion")
    assert set(cleared) == {"alice", "bob"}
    assert json.loads((a / "mcp-needs-auth-cache.json").read_text()) == {"linear": {"t": 2}}
    assert json.loads((b / "mcp-needs-auth-cache.json").read_text()) == {}

def test_clear_no_match_returns_empty(fake_root):
    cfg_root, _ = fake_root
    a = cfg_root / "alice"; a.mkdir()
    (a / "mcp-needs-auth-cache.json").write_text(json.dumps({"linear": {"t": 2}}))
    assert mcp_auth.clear_needs_auth_cache("notion") == []
