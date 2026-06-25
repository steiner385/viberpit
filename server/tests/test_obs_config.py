from server.config import get_settings


def test_log_defaults():
    s = get_settings()
    assert s.log_level == "INFO"
    assert s.client_log_level == "warn"
    assert s.log_dir  # non-empty default


def test_log_env_override(monkeypatch):
    monkeypatch.setenv("CCSWITCH_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("CCSWITCH_LOG_DIR", "/tmp/ccswitch-logs")
    s = get_settings()
    assert s.log_level == "DEBUG"
    assert s.log_dir == "/tmp/ccswitch-logs"
