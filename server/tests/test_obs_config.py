from server.config import get_settings
from server.fsmounts import read_mounts


def test_log_defaults():
    s = get_settings()
    assert s.log_level == "INFO"
    assert s.client_log_level == "warn"
    # #1010: never the sshfs-shared account store. "" is a legitimate answer now
    # (stdout-only), but only on a host with no local writable candidate at all.
    assert ".claude-accounts" not in s.log_dir
    if read_mounts():
        assert s.log_dir  # non-empty default


def test_log_env_override(monkeypatch):
    monkeypatch.setenv("CCSWITCH_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("CCSWITCH_LOG_DIR", "/tmp/viberpit-logs")
    s = get_settings()
    assert s.log_level == "DEBUG"
    assert s.log_dir == "/tmp/viberpit-logs"
