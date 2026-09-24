"""#1010: the JSON log directory must be per-HOST, never the sshfs-shared account store.

The pre-fix default asked ``os.access("/var/log", os.W_OK)`` — writability of the
root-owned *parent* — so under ``User=tony`` it was always false and every process
fell through to ``~/.claude-accounts/logs``. On the fleet that is an sshfs mount of
dobby's directory, so rocky's daemon wrote into dobby's file and both hosts rotated
it; the daemon's own logs for the #1009 incident were unrecoverable.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import pytest

from server import hostpaths
from server.fsmounts import read_mounts
from server.obs import logdir

ROOT = Path(__file__).resolve().parents[2]
#: The one env override these tests drive. Hoisted so the legacy spelling appears
#: once in this file (scripts/brand_ratchet.py, #1591).
LOG_DIR_ENV = "VIBERPIT_LOG_DIR"


@pytest.fixture(autouse=True)
def _forget_memoised_answer():
    """default_log_dir() caches its no-argument answer; every test here moves the
    candidates, so the cache must not carry across (or in from the real host)."""
    logdir.reset_cache_for_tests()
    yield
    logdir.reset_cache_for_tests()


def _pin_candidates(monkeypatch, tmp_path: Path) -> None:
    """Point every candidate at *tmp_path* so the test does not depend on the host."""
    monkeypatch.setattr(hostpaths, "LOG_DIR", str(tmp_path / "var-log" / "viberpit"))
    monkeypatch.setattr(hostpaths, "STATE_DIR", str(tmp_path / "var-lib" / "viberpit"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))


# ---------------------------------------------------------------------------
# The regression itself
# ---------------------------------------------------------------------------


def test_default_never_lands_in_the_shared_account_store(monkeypatch, tmp_path):
    """The whole point of #1010 — checked against this host's real mount table."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert ".claude-accounts" not in logdir.default_log_dir()


def test_unwritable_var_log_no_longer_forces_a_shared_fallback(monkeypatch, tmp_path):
    """The old predicate asked about /var/log's PARENT, which a non-root process never owns.

    Here /var/log itself is unwritable — the pre-``LogsDirectory=`` host — and the
    answer must still be a host-local directory rather than the account store.
    """
    var_log = tmp_path / "var-log"
    var_log.mkdir()
    var_log.chmod(0o555)
    monkeypatch.setattr(hostpaths, "LOG_DIR", str(var_log / "viberpit"))
    monkeypatch.setattr(hostpaths, "STATE_DIR", str(tmp_path / "var-lib" / "viberpit"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    try:
        chosen = logdir.default_log_dir()
    finally:
        var_log.chmod(0o755)
    assert chosen == str(tmp_path / "var-lib" / "viberpit" / "logs")


# ---------------------------------------------------------------------------
# Candidate order
# ---------------------------------------------------------------------------


def test_logs_directory_wins_when_the_unit_has_created_it(monkeypatch, tmp_path):
    """``LogsDirectory=viberpit`` is the deployed answer, so it must be preferred."""
    _pin_candidates(monkeypatch, tmp_path)
    (tmp_path / "var-log" / "viberpit").mkdir(parents=True)
    assert logdir.default_log_dir() == str(tmp_path / "var-log" / "viberpit")


def test_falls_through_to_xdg_when_no_system_dir_is_writable(monkeypatch, tmp_path):
    root_owned = tmp_path / "root-owned"
    root_owned.mkdir()
    root_owned.chmod(0o555)
    monkeypatch.setattr(hostpaths, "LOG_DIR", str(root_owned / "viberpit"))
    monkeypatch.setattr(hostpaths, "STATE_DIR", str(root_owned / "state"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    try:
        assert logdir.default_log_dir() == str(tmp_path / "xdg" / "viberpit" / "logs")
    finally:
        root_owned.chmod(0o755)


def test_a_candidate_on_a_share_is_skipped_even_when_writable(monkeypatch, tmp_path):
    """Writable is not enough — rocky could write dobby's file over sshfs just fine."""
    _pin_candidates(monkeypatch, tmp_path)
    (tmp_path / "var-log" / "viberpit").mkdir(parents=True)
    mounts = ((str(tmp_path), "ext4"), (str(tmp_path / "var-log"), "fuse.sshfs"))
    assert logdir.default_log_dir(mounts=mounts) == str(
        tmp_path / "var-lib" / "viberpit" / "logs")


def test_no_local_candidate_degrades_to_stdout_only(monkeypatch, tmp_path):
    _pin_candidates(monkeypatch, tmp_path)
    assert logdir.default_log_dir(mounts=((str(tmp_path), "nfs4"),)) == ""


def test_can_write_does_not_create_the_candidate(tmp_path):
    """Refuse before you write: probing must leave nothing behind on a rejected path."""
    candidate = tmp_path / "a" / "b" / "logs"
    assert logdir._can_write(candidate) is True
    assert not (tmp_path / "a").exists()


def test_can_write_rejects_a_file_where_the_directory_should_be(tmp_path):
    blocker = tmp_path / "logs"
    blocker.write_text("not a directory")
    assert logdir._can_write(blocker) is False


# ---------------------------------------------------------------------------
# is_shared_dir — the same rule server/codex/process.py applies to CODEX_HOME
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fstype,shared",
    [("ext4", False), ("btrfs", False), ("overlay", False), ("tmpfs", False),
     ("fuse.sshfs", True), ("nfs4", True), ("cifs", True)],
)
def test_is_shared_dir_follows_the_network_fstype_rule(fstype, shared):
    assert logdir.is_shared_dir("/x/y", mounts=(("/x", fstype),)) is shared


def test_is_shared_dir_fails_closed_on_an_unreadable_mount_table():
    """"We could not tell" is not evidence of locality — same rule as CODEX_HOME."""
    assert logdir.is_shared_dir("/x/y", mounts=()) is True


def test_is_shared_dir_resolves_symlinks_before_answering(tmp_path):
    """``~/.local/state`` pointed at the fleet's share is the shape the docs warn about."""
    share = tmp_path / "share"
    share.mkdir()
    link = tmp_path / "state"
    link.symlink_to(share)
    mounts = ((str(tmp_path), "ext4"), (str(share), "fuse.sshfs"))
    assert logdir.is_shared_dir(link / "viberpit" / "logs", mounts=mounts) is True


# ---------------------------------------------------------------------------
# configure_logging honours both rules
# ---------------------------------------------------------------------------


def _restore_logging(before) -> None:
    from server.obs import errsink
    errsink.reset_for_tests()  # configure_logging() installs the error sink singleton
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler not in before:
            handler.close()
            root.removeHandler(handler)


def test_file_is_named_per_process_so_edge_and_daemon_never_share_one(monkeypatch, tmp_path):
    """A RotatingFileHandler renames the live file at rollover; two writers on one is the bug."""
    from server.obs import jsonlog

    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
    before = list(logging.getLogger().handlers)
    before_process = jsonlog.PROCESS
    monkeypatch.setattr(jsonlog, "_configured", False)
    try:
        jsonlog.configure_logging(process="daemon")
        logging.getLogger("t").warning("hello")
        assert (tmp_path / "viberpit-daemon.jsonl").exists()
        assert not (tmp_path / "viberpit-edge.jsonl").exists()
    finally:
        jsonlog.PROCESS = before_process
        _restore_logging(before)


def test_an_operator_set_shared_log_dir_gets_no_file_handler(monkeypatch, tmp_path):
    from server.obs import jsonlog

    monkeypatch.setenv(LOG_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(jsonlog, "is_shared_dir", lambda path: True)
    before = list(logging.getLogger().handlers)
    before_process = jsonlog.PROCESS
    monkeypatch.setattr(jsonlog, "_configured", False)
    try:
        jsonlog.configure_logging(process="daemon")
        added = [h for h in logging.getLogger().handlers if h not in before]
        assert added, "configure_logging attached nothing at all"
        assert not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in added)
        # The journal still gets every line — that is what makes refusing the file safe.
        assert any(type(h) is logging.StreamHandler for h in added)
        assert not list(tmp_path.iterdir())
    finally:
        jsonlog.PROCESS = before_process
        _restore_logging(before)


# ---------------------------------------------------------------------------
# The units declare the directory the default prefers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit", [
    "viberpit.service",
    "viberpit-daemon.service",
    "viberpit-daemon-remote.service",
])
def test_units_declare_a_per_host_logs_directory(unit):
    """Locality as a property of the unit, exactly like StateDirectory= for #1005."""
    assert "LogsDirectory=viberpit\n" in (ROOT / "deploy" / unit).read_text()


# ---------------------------------------------------------------------------
# Both Settings variants share one answer (constitution VI)
# ---------------------------------------------------------------------------


def test_settings_default_routes_through_default_log_dir(monkeypatch, tmp_path):
    from server.config import get_settings

    monkeypatch.delenv(LOG_DIR_ENV, raising=False)
    _pin_candidates(monkeypatch, tmp_path)
    (tmp_path / "var-log" / "viberpit").mkdir(parents=True)
    assert get_settings().log_dir == str(tmp_path / "var-log" / "viberpit")


def test_env_override_still_wins(monkeypatch):
    from server.config import get_settings

    monkeypatch.setenv(LOG_DIR_ENV, "/tmp/viberpit-logs-explicit")
    assert get_settings().log_dir == "/tmp/viberpit-logs-explicit"


def test_the_live_host_can_answer_the_mount_question():
    """Guards the fail-closed branch: a host with no readable mount table logs nowhere."""
    assert read_mounts(), "/proc/mounts unreadable — is_shared_dir would refuse every dir"
