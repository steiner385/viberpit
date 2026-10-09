# server/tests/test_mcp_auth_pty.py
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


def test_picker_prefers_needs_auth_account(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_auth.accounts, "_anthropic_account_names", lambda: ["alice", "bob"])
    monkeypatch.setattr(mcp_auth.configdir, "compose", lambda n: tmp_path / n)
    name, cfg = default_account_picker("notion", ["bob", "alice"])
    assert name == "bob"  # first preferred account
    assert cfg == tmp_path / "bob"


def test_picker_raises_when_no_account(monkeypatch):
    monkeypatch.setattr(mcp_auth.accounts, "_anthropic_account_names", lambda: [])
    with pytest.raises(McpAuthError):
        default_account_picker("notion", [])


def test_picker_falls_back_to_anthropic_account(monkeypatch, tmp_path):
    """No preferred account but an anthropic account exists → return it."""
    monkeypatch.setattr(mcp_auth.accounts, "_anthropic_account_names", lambda: ["alice"])
    monkeypatch.setattr(mcp_auth.configdir, "compose", lambda n: tmp_path / n)
    name, cfg = default_account_picker("notion", [])
    assert name == "alice"
    assert cfg == tmp_path / "alice"
