import pytest
from server.sessions import claude_sdk_factory


@pytest.mark.asyncio
async def test_extra_env_merged(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, options):
            captured["env"] = options.env

    class FakeOpts:
        def __init__(self, **kw):
            self.env = kw["env"]

    monkeypatch.setattr(
        claude_sdk_factory, "_import_sdk", lambda: (FakeClient, FakeOpts), raising=False
    )
    await claude_sdk_factory.real_sdk_factory(
        "/tmp",
        "/cfg",
        None,
        "interactive",
        None,
        extra_env={"DISPLAY": ":100", "XAUTHORITY": "/x"},
    )
    assert captured["env"]["DISPLAY"] == ":100"
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == "/cfg"


def test_session_tmpdir_defaults_under_home_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
    monkeypatch.delenv("VIBERPIT_SESSION_TMPDIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    env = claude_sdk_factory.session_tmpdir_env()
    want = tmp_path / ".cache" / "viberpit" / "claude-tmp"
    assert env == {"CLAUDE_CODE_TMPDIR": str(want)}
    assert want.is_dir()


def test_session_tmpdir_override_and_operator_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
    monkeypatch.setenv("VIBERPIT_SESSION_TMPDIR", str(tmp_path / "t"))
    assert claude_sdk_factory.session_tmpdir_env() == {"CLAUDE_CODE_TMPDIR": str(tmp_path / "t")}
    monkeypatch.setenv("CLAUDE_CODE_TMPDIR", "/elsewhere")
    assert claude_sdk_factory.session_tmpdir_env() == {}


def test_session_tmpdir_fails_open(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_TMPDIR", raising=False)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("VIBERPIT_SESSION_TMPDIR", str(blocker / "sub"))
    assert claude_sdk_factory.session_tmpdir_env() == {}
