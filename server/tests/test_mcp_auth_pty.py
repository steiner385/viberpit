# server/tests/test_mcp_auth_pty.py
import json
import pytest
from server import mcp_auth
from server.mcp_auth import _extract_url, default_account_picker, McpAuthError


def test_extract_url_grabs_first_https_token():
    txt = "Open the following URL to authorize:\n  https://mcp.notion.com/authorize?state=abc \nPaste the redirect URL:"
    assert _extract_url(txt) == "https://mcp.notion.com/authorize?state=abc"


def test_extract_url_strips_osc8_escape_bytes():
    # Real output wraps the URL in an OSC-8 hyperlink; the plain URL is the
    # display text but escape bytes (\x1b ... \x07) surround it.
    txt = "Visit this URL to authorize:\r\n  \x1b]8;;https://backboard.railway.com/oauth/auth?state=s\x07https://backboard.railway.com/oauth/auth?state=s\x1b]8;;\x07\r\n"
    assert _extract_url(txt) == "https://backboard.railway.com/oauth/auth?state=s"


def test_extract_url_none_when_absent():
    assert _extract_url("connecting...\n") is None


def test_resolve_login_key_returns_full_plugin_key(monkeypatch, tmp_path):
    cfg_root = tmp_path / "cfg"; cfg_root.mkdir()
    (cfg_root / "alice").mkdir()
    (cfg_root / "alice" / "mcp-needs-auth-cache.json").write_text(
        json.dumps({"plugin:railway:railway": {"t": 1}}))
    monkeypatch.setattr(mcp_auth.paths, "cfg_root", lambda: cfg_root)
    assert mcp_auth.resolve_login_key("railway") == "plugin:railway:railway"


def test_resolve_login_key_falls_back_to_short_name(monkeypatch, tmp_path):
    cfg_root = tmp_path / "cfg"; cfg_root.mkdir()
    monkeypatch.setattr(mcp_auth.paths, "cfg_root", lambda: cfg_root)
    assert mcp_auth.resolve_login_key("customserver") == "customserver"


def test_picker_prefers_needs_auth_account(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_auth.scan, "_needs_auth_by_server", lambda: {"notion": ["bob", "alice"]})
    monkeypatch.setattr(mcp_auth.configdir, "compose", lambda n: tmp_path / n)
    name, cfg = default_account_picker("notion")
    assert name == "bob"  # first in the needs-auth list
    assert cfg == tmp_path / "bob"


def test_picker_raises_when_no_account(monkeypatch):
    monkeypatch.setattr(mcp_auth.scan, "_needs_auth_by_server", lambda: {})
    monkeypatch.setattr(mcp_auth.accounts, "_anthropic_account_names", lambda: [])
    with pytest.raises(McpAuthError):
        default_account_picker("notion")
