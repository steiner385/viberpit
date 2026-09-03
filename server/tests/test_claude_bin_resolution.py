"""Tests for server.sessions.claude_bin.resolve_claude_bin — the single source of
truth for *which* `claude` executable ccswitch runs (picker probe + SDK sessions)."""
import logging
import os
import shutil

import pytest

from server.sessions import claude_bin
from server.sessions.claude_bin import resolve_claude_bin


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    """Point ``~`` at an empty dir so the well-known-location fallback never
    finds the developer's real ``~/.local/bin/claude`` and turns a "nothing
    resolves" test green by accident."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _make_exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


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


# --- well-known install locations: a systemd unit's PATH is not the user's ------
#
# The daemon runs under a minimal systemd PATH that lacks ~/.local/bin, so the
# bare `which("claude")` misses the native installer's binary and the SDK falls
# back to its stale bundled CLI (dobby, 2026-09-03). Resolution must not depend
# on a per-host PATH line that nobody remembers to add to daemon.env.


def test_resolve_falls_back_to_local_bin_when_not_on_path(monkeypatch, isolated_home):
    monkeypatch.delenv("CCSWITCH_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda c: None)
    binary = _make_exe(isolated_home / ".local" / "bin" / "claude")
    assert resolve_claude_bin() == str(binary)


def test_resolve_falls_back_to_newest_installed_version(monkeypatch, isolated_home):
    # No ~/.local/bin/claude symlink at all → pick the highest SEMANTIC version
    # under the native installer's versions store. "2.1.9" sorts lexically after
    # "2.1.259"; numeric ordering must win.
    monkeypatch.delenv("CCSWITCH_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda c: None)
    versions = isolated_home / ".local" / "share" / "claude" / "versions"
    _make_exe(versions / "2.1.9")
    newest = _make_exe(versions / "2.1.259")
    _make_exe(versions / "2.1.100")
    assert resolve_claude_bin() == str(newest)


def test_resolve_skips_dangling_local_bin_symlink(monkeypatch, isolated_home):
    # A half-finished `claude update` can leave ~/.local/bin/claude pointing at a
    # version dir that no longer exists; skip it rather than pin a dead path.
    monkeypatch.delenv("CCSWITCH_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda c: None)
    local_bin = isolated_home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    (local_bin / "claude").symlink_to(isolated_home / "gone")
    newest = _make_exe(isolated_home / ".local" / "share" / "claude" / "versions" / "2.1.259")
    assert resolve_claude_bin() == str(newest)


def test_path_claude_wins_over_well_known_locations(monkeypatch, isolated_home):
    # PATH (or CCSWITCH_CLAUDE_BIN) stays authoritative; the well-known scan is
    # only a fallback so an operator override is never silently shadowed.
    monkeypatch.delenv("CCSWITCH_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda c: "/opt/claude" if c == "claude" else None)
    _make_exe(isolated_home / ".local" / "bin" / "claude")
    assert resolve_claude_bin() == "/opt/claude"


def test_well_known_fallback_still_subject_to_floor(monkeypatch, isolated_home, caplog):
    monkeypatch.delenv("CCSWITCH_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda c: None)
    _make_exe(isolated_home / ".local" / "bin" / "claude")
    monkeypatch.setattr(claude_bin, "_probe_version", lambda p: (2, 1, 162))
    monkeypatch.setattr(claude_bin, "_min_version", lambda: (2, 1, 183))
    claude_bin._warned.clear()
    with caplog.at_level(logging.ERROR):
        assert resolve_claude_bin() is None


def test_well_known_fallback_ignores_non_executable(monkeypatch, isolated_home):
    monkeypatch.delenv("CCSWITCH_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(shutil, "which", lambda c: None)
    stub = isolated_home / ".local" / "bin" / "claude"
    stub.parent.mkdir(parents=True)
    stub.write_text("not executable\n")
    os.chmod(stub, 0o644)
    assert resolve_claude_bin() is None


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
