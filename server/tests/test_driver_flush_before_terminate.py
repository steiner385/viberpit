"""Teardown must not SIGTERM the CLI before it has written its final reply (#1012).

``_persist_status('complete')`` schedules ``aclose()`` in the same tick the turn's
ResultMessage arrives, and ``aclose()`` goes straight to the SDK client's
``__aexit__`` — terminate, then kill. The CLI writes its transcript lazily, so that
window can lose the last assistant message (it lost a real answer in #1009). The
driver knows the uuid the CLI named that message with, and the CLI writes the
transcript entry under the same uuid, so the wait is on the observable fact.
"""
import asyncio
import json
import os

from claude_agent_sdk import TextBlock

from server.sessions.claude_driver import ClaudeAgentDriver, _scan_transcript_tail
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes import fake_sdk as f
from server.tests.fakes.fake_sdk import FakeSDKClient

ASSISTANT_UUID = "a-final-uuid"


def _transcript(cfg: str, session_id: str) -> str:
    d = os.path.join(cfg, "projects", "-repo")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{session_id}.jsonl")


def _append_entry(path: str, uuid: str) -> None:
    with open(path, "a") as fh:
        fh.write(json.dumps({"type": "assistant", "uuid": uuid,
                             "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n")


class _FlushWatchingClient(FakeSDKClient):
    """Records whether the transcript held the final reply when __aexit__ ran —
    i.e. whether teardown terminated the CLI before or after the flush."""

    def __init__(self, *, transcript: str, **kw):
        super().__init__(**kw)
        self._transcript = transcript
        self.exited = False
        self.flushed_at_exit: bool | None = None

    async def __aexit__(self, *exc):
        try:
            with open(self._transcript) as fh:
                self.flushed_at_exit = ASSISTANT_UUID in fh.read()
        except OSError:
            self.flushed_at_exit = False
        self.exited = True
        return await super().__aexit__(*exc)


def _build(tmp_path, client, *, flush_grace=2.0):
    d = ClaudeAgentDriver("s1", "primary", "/repo", str(tmp_path), mode="interactive",
                          sdk_factory=lambda *a, **k: client, registry=PermissionRegistry(),
                          identity="me")
    d._close_grace = 0.2
    d._flush_grace = flush_grace
    return d


def _turn():
    """One ordinary turn: a consolidated top-level assistant message, then a result."""
    return [f.sys_init(session_id="sess-1"),
            f.assistant(TextBlock(text="done"), uuid=ASSISTANT_UUID),
            f.result(uuid="r1")]


def test_aexit_waits_until_the_final_reply_is_on_disk(tmp_path):
    path = _transcript(str(tmp_path), "sess-1")
    _append_entry(path, "some-earlier-uuid")   # the file exists, the reply does not

    async def run():
        client = _FlushWatchingClient(messages=_turn(), transcript=path, startup_frames=[])
        d = _build(tmp_path, client)
        await d.start()
        await d.send_prompt("go")
        for _ in range(200):                   # let the turn settle
            await asyncio.sleep(0.005)
            if not d._turn_active and d._last_assistant_uuid == ASSISTANT_UUID:
                break
        # The CLI flushes LATE — after teardown has already begun, as in #1009.
        async def late_flush():
            await asyncio.sleep(0.2)
            _append_entry(path, ASSISTANT_UUID)
        flusher = asyncio.create_task(late_flush())
        await asyncio.wait_for(d.aclose(), timeout=5.0)
        await flusher
        return client

    client = asyncio.run(run())
    assert client.exited is True
    assert client.flushed_at_exit is True, (
        "the CLI was terminated before it had written its final assistant message")


def test_teardown_is_not_blocked_when_the_cli_never_flushes(tmp_path, caplog):
    path = _transcript(str(tmp_path), "sess-1")
    _append_entry(path, "some-earlier-uuid")

    async def run():
        client = _FlushWatchingClient(messages=_turn(), transcript=path, startup_frames=[])
        d = _build(tmp_path, client, flush_grace=0.3)
        await d.start()
        await d.send_prompt("go")
        for _ in range(200):
            await asyncio.sleep(0.005)
            if not d._turn_active and d._last_assistant_uuid == ASSISTANT_UUID:
                break
        await asyncio.wait_for(d.aclose(), timeout=5.0)   # must not hang
        return client

    client = asyncio.run(run())
    assert client.exited is True
    assert client.flushed_at_exit is False
    # The give-up names the uuid, so a recurring loss is diagnosable from the journal.
    assert any(ASSISTANT_UUID in r.getMessage() for r in caplog.records
               if r.levelname == "WARNING")


def test_no_flush_wait_when_the_stream_already_ended(tmp_path):
    """A dead CLI has nobody left to flush — teardown must not pay the grace."""
    path = _transcript(str(tmp_path), "sess-1")
    _append_entry(path, "some-earlier-uuid")

    async def run():
        client = _FlushWatchingClient(messages=_turn(), transcript=path, startup_frames=[])
        d = _build(tmp_path, client, flush_grace=30.0)
        await d.start()
        await d.send_prompt("go")
        for _ in range(200):
            await asyncio.sleep(0.005)
            if not d._turn_active and d._last_assistant_uuid == ASSISTANT_UUID:
                break
        d._stream_ended = True
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(d.aclose(), timeout=5.0)
        return asyncio.get_running_loop().time() - started

    assert asyncio.run(run()) < 2.0


def test_sub_agent_replies_are_not_what_teardown_waits_for(tmp_path):
    """A Task sub-agent's message is not the turn's answer (and lives in a sidechain)."""

    async def run():
        msgs = [f.sys_init(session_id="sess-1"),
                f.assistant(uuid="sub-uuid", parent_tool_use_id="toolu_1"),
                f.result(uuid="r1")]
        client = FakeSDKClient(messages=msgs, startup_frames=[])
        d = _build(tmp_path, client)
        await d.start()
        await d.send_prompt("go")
        for _ in range(200):
            await asyncio.sleep(0.005)
            if not d._turn_active:
                break
        await d.aclose()
        return d._last_assistant_uuid

    assert asyncio.run(run()) is None


def test_tail_scan_skips_the_read_when_the_file_has_not_grown(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("nothing interesting\n")
    found, size = _scan_transcript_tail(str(path), "needle", last_size=-1)
    assert (found, size) == (False, path.stat().st_size)
    # Same size → the scan short-circuits, even if the content changed underneath.
    path.write_text("needle_interesting!\n")
    assert path.stat().st_size == size
    assert _scan_transcript_tail(str(path), "needle", last_size=size) == (False, size)
    # A grown file is re-read.
    with open(path, "a") as fh:
        fh.write("needle\n")
    found, _ = _scan_transcript_tail(str(path), "needle", last_size=size)
    assert found is True


def test_tail_scan_is_quiet_about_a_missing_transcript(tmp_path):
    assert _scan_transcript_tail(str(tmp_path / "gone.jsonl"), "x", last_size=-1) == (False, -1)
