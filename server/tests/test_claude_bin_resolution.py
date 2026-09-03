"""Tests for server.sessions.claude_bin.resolve_claude_bin — the single source of
truth for *which* `claude` executable ccswitch runs (picker probe + SDK sessions)."""
import logging
import shutil

from server.sessions import claude_bin
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


# --- version floor: never pin a binary older than the SDK's bundled CLI --------


def test_resolve_rejects_binary_below_floor(monkeypatch, caplog):
    # A resolvable but stale system claude (older than the bundled fallback) must
    # NOT be pinned — returning None lets the SDK use its newer bundled CLI.
    monkeypatch.delenv("CCSWITCH_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda c: "/opt/claude" if c == "claude" else None)
    monkeypatch.setattr(claude_bin, "_probe_version", lambda p: (2, 1, 162))
    monkeypatch.setattr(claude_bin, "_min_version", lambda: (2, 1, 183))
    with caplog.at_level(logging.ERROR):
        assert resolve_claude_bin() is None
    assert any("below" in r.message.lower() for r in caplog.records)


def test_resolve_keeps_binary_at_or_above_floor(monkeypatch):
    monkeypatch.delenv("CCSWITCH_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda c: "/opt/claude" if c == "claude" else None)
    monkeypatch.setattr(claude_bin, "_probe_version", lambda p: (2, 1, 251))
    monkeypatch.setattr(claude_bin, "_min_version", lambda: (2, 1, 183))
    assert resolve_claude_bin() == "/opt/claude"


def test_resolve_keeps_binary_when_version_unprobeable(monkeypatch):
    # An unknowable version must NOT be rejected — that would regress today's
    # behavior (trust whatever resolves) for a binary that may well be current.
    monkeypatch.delenv("CCSWITCH_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda c: "/opt/claude" if c == "claude" else None)
    monkeypatch.setattr(claude_bin, "_probe_version", lambda p: None)
    assert resolve_claude_bin() == "/opt/claude"


def test_resolve_warns_when_nothing_found(monkeypatch, caplog):
    monkeypatch.delenv("CCSWITCH_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda c: None)
    claude_bin._warned.clear()
    with caplog.at_level(logging.WARNING):
        assert resolve_claude_bin() is None
    assert any("bundled" in r.message.lower() for r in caplog.records)


def test_min_version_env_override(monkeypatch):
    monkeypatch.setenv("CCSWITCH_CLAUDE_MIN_VERSION", "9.9.9")
    assert claude_bin._min_version() == (9, 9, 9)
