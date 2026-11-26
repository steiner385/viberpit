"""#3501: a resumed session's SessionStart env file can carry NUL bytes (a hook
appending while the CLI truncates). The CLI sources it ahead of EVERY Bash call,
and a NUL makes each one fail before it runs — so the driver scrubs the file
before the resumed CLI starts.
"""
import asyncio
import os

from server.sessions.claude_driver import ClaudeAgentDriver
from server.sessions.permissions import PermissionRegistry
from server.sessions.session_env_repair import repair_session_env
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient

SID = "7f617140-9cff-43b2-bda2-8db38bf985da"
LINE = f"export IMPECCABLE_SESSION_ID='{SID}'\n".encode()


def _env_file(config_dir, name="sessionstart-hook-2.sh", data=LINE + b"\x00" * 68 + LINE,
              sid=SID):
    d = os.path.join(config_dir, "session-env", sid)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def test_strips_nul_bytes_and_keeps_content(tmp_path):
    path = _env_file(str(tmp_path))
    assert repair_session_env(str(tmp_path), SID) == 1
    with open(path, "rb") as fh:
        assert fh.read() == LINE + LINE


def test_clean_file_is_left_untouched(tmp_path):
    path = _env_file(str(tmp_path), data=LINE)
    before = os.stat(path).st_mtime_ns
    assert repair_session_env(str(tmp_path), SID) == 0
    assert os.stat(path).st_mtime_ns == before


def test_only_the_named_sessions_dir_is_touched(tmp_path):
    other = _env_file(str(tmp_path), sid="other-sid")
    assert repair_session_env(str(tmp_path), SID) == 0
    with open(other, "rb") as fh:
        assert b"\x00" in fh.read()


def test_missing_dir_and_bad_ids_are_noops(tmp_path):
    assert repair_session_env(str(tmp_path), SID) == 0
    assert repair_session_env(str(tmp_path), "") == 0
    assert repair_session_env("", SID) == 0
    # A traversal-shaped id never escapes session-env/.
    _env_file(str(tmp_path), sid="victim")
    assert repair_session_env(str(tmp_path), "../session-env/victim") == 0


def test_driver_start_scrubs_env_file_on_resume(tmp_path):
    cfg = str(tmp_path)
    path = _env_file(cfg)

    def factory(cwd, config_dir, can_use_tool, mode, resume, *, on_pre_compact=None):
        return FakeSDKClient([f.sys_init(SID), f.result(session_id=SID, uuid="t1")],
                             on_can_use_tool=can_use_tool)

    async def run():
        d = ClaudeAgentDriver("s1", "primary", "/repo", cfg, mode="interactive",
                              sdk_factory=factory, registry=PermissionRegistry(),
                              identity="me", resume=SID)
        await d.start()
        await d.aclose()

    asyncio.run(run())
    with open(path, "rb") as fh:
        assert b"\x00" not in fh.read()
