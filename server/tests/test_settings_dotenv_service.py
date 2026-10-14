"""A systemd-managed process never reads ``.env`` (#3073).

The edge's working directory is the deploy checkout, writable by the operator's user
and so by every agent session. ``get_settings()`` builds a fresh Settings per call, so
a ``.env`` there would let a session change security settings on the running edge.
Under systemd the root-owned EnvironmentFile is the only source; dev runs keep ``.env``.
"""
import pytest

from server.config import get_settings


@pytest.fixture
def hostile_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "VIBERPIT_ALLOW_UNAUTHENTICATED=true\n"
        "VIBERPIT_TAILNET_ALLOWLIST=attacker@example.com\n"
    )
    monkeypatch.chdir(tmp_path)
    for name in ("VIBERPIT_ALLOW_UNAUTHENTICATED", "CCSWITCH_ALLOW_UNAUTHENTICATED",
                 "VIBERPIT_TAILNET_ALLOWLIST", "CCSWITCH_TAILNET_ALLOWLIST"):
        monkeypatch.delenv(name, raising=False)


def test_systemd_service_ignores_dotenv(hostile_dotenv, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef0123456789abcdef")
    settings = get_settings()
    assert settings.allow_unauthenticated is False
    assert settings.tailnet_allowlist == ""


def test_dev_run_still_reads_dotenv(hostile_dotenv, monkeypatch):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    settings = get_settings()
    assert settings.allow_unauthenticated is True
    assert settings.tailnet_allowlist == "attacker@example.com"


def test_service_environment_still_applies(hostile_dotenv, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("VIBERPIT_TAILNET_ALLOWLIST", "tony@example.com")
    assert get_settings().tailnet_allowlist == "tony@example.com"
