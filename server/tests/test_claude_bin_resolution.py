"""Tests for server.sessions.claude_bin.resolve_claude_bin — the single source of
truth for *which* `claude` executable ccswitch runs (picker probe + SDK sessions)."""
import shutil

from server.sessions.claude_bin import resolve_claude_bin


def test_resolve_uses_path_claude_by_default(monkeypatch):
    monkeypatch.delenv("CCSWITCH_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda c: "/opt/claude" if c == "claude" else None)
    assert resolve_claude_bin() == "/opt/claude"


def test_resolve_honors_env_override_first_token_only(monkeypatch):
    # CCSWITCH_CLAUDE_BIN may carry flags; only the executable is the cli_path.
    monkeypatch.setenv("CCSWITCH_CLAUDE_BIN", "/custom/claude --dangerously-skip-permissions")
    monkeypatch.setattr(shutil, "which", lambda c: c if c == "/custom/claude" else None)
    assert resolve_claude_bin() == "/custom/claude"


def test_resolve_returns_none_when_nothing_found(monkeypatch):
    monkeypatch.delenv("CCSWITCH_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda c: None)
    assert resolve_claude_bin() is None


def test_resolve_finds_existing_abs_path_not_on_path(monkeypatch, tmp_path):
    # An absolute path that exists+executable but which() misses (empty PATH).
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setenv("CCSWITCH_CLAUDE_BIN", str(binary))
    monkeypatch.setattr(shutil, "which", lambda c: None)
    assert resolve_claude_bin() == str(binary)
