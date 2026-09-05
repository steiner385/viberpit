"""`CodexAgentDriver` against a real JSONL app-server peer.

Every test here drives the whole stack the way production does — driver →
manager → protocol → JSONL bytes → :class:`FakeCodexAppServer` — because the
properties worth pinning are all boundary properties: which frame goes on the
wire, which normalized event comes back, and what happens to a turn when the
process running it disappears. A mocked client would assert the driver's own
opinion of the protocol rather than the protocol.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from server.codex import schema
from server.codex.capabilities import CodexAvailability
from server.codex.driver import (
    CodexAgentDriver,
    CodexAgentDriverFactory,
    CodexAttachmentRejected,
    CodexRunnerUnavailable,
)
from server.codex.manager import (
    RECOVER_VIA_THREAD_READ,
    CodexRunnerManager,
    RunnerNotice,
    RunnerNoticeKind,
)
from server.codex.redaction import Redacted
from server.sessions import events as ev
from server.sessions.completion import COMPLETION_MARKER
from server.sessions.engine import AgentDriver, AgentDriverConfig, AgentEngine
from server.sessions.model_request import MODEL_REQUEST_INSTRUCTION
from server.sessions.permissions import PermissionRegistry
from server.tests.fakes.fake_codex_app_server import FakeCodexAppServer

ACCOUNT = "openai-main"
SESSION = "sess-1"
THREAD = "thread-1"


# ---------------------------------------------------------------------------
# Wire fixtures
# ---------------------------------------------------------------------------


def thread_payload(thread_id: str = THREAD, cwd: str = "/work") -> dict[str, Any]:
    return {
        "id": thread_id,
        "cwd": cwd,
        "status": {"type": "idle"},
        "modelProvider": "openai",
        "createdAt": 1,
        "updatedAt": 2,
        "model": "gpt-5.1-codex",
    }


def thread_config(
    thread_id: str = THREAD,
    *,
    sandbox: str = "workspaceWrite",
    approval_policy: str = "untrusted",
    cwd: str = "/work",
) -> dict[str, Any]:
    return {
        "thread": thread_payload(thread_id, cwd),
        "model": "gpt-5.1-codex",
        "sandbox": {"type": sandbox},
        "approvalPolicy": approval_policy,
    }


def turn_payload(turn_id: str, status: str = "inProgress") -> dict[str, Any]:
    return {"id": turn_id, "status": status, "items": []}


class FakeCodexProcess:
    """A `CodexProcessHandle` over an in-process fake app-server."""

    def __init__(self, account: str, home: Path) -> None:
        self.account = account
        self.home = home
        self.pid = 4242
        self.server = FakeCodexAppServer()
        self.server.rule(
            "initialize", result=FakeCodexAppServer.default_initialize_response()
        )
        self.server.rule("account/login/start", result={"type": "chatgptAuthTokens"})
        self._exited = asyncio.Event()
        self._rc = 0

    @property
    def transport(self) -> Any:
        return self.server.transport

    async def start(self) -> None:
        self.server.start_autopilot()

    async def wait(self) -> int:
        await self._exited.wait()
        return self._rc

    async def stop(self, timeout: float = 5.0) -> None:
        self._exited.set()
        await self.server.stop_autopilot()
        self.server.eof()

    def stderr_tail(self) -> str:
        return ""


async def _credentials(account: str) -> schema.ExternalAuthLoginParams:
    return schema.ExternalAuthLoginParams(
        access_token=Redacted("token"), chatgpt_account_id=Redacted("acct")
    )


class Harness:
    """A started runner, a live driver, and everything the driver emitted."""

    def __init__(self, tmp_path: Path) -> None:
        self._tmp = tmp_path
        self.processes: list[FakeCodexProcess] = []
        self.manager: CodexRunnerManager
        self.server: FakeCodexAppServer
        self.driver: CodexAgentDriver
        self.registry = PermissionRegistry()
        self.events: list[Any] = []
        self.statuses: list[str] = []
        self.engine_ids: list[tuple[str, str]] = []
        self.context_usage: list[tuple[int, int]] = []
        self.failures: list[str] = []
        self._collector: asyncio.Task[None] | None = None

    def _process(self, *, account: str, binary: str, home: Path) -> FakeCodexProcess:
        child = FakeCodexProcess(account, home)
        self.processes.append(child)
        return child

    async def boot(
        self,
        *,
        mode: str = "interactive",
        resume: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
        start: bool = True,
    ) -> CodexAgentDriver:
        self.manager = CodexRunnerManager(
            availability=CodexAvailability(available=True, binary_path="/usr/bin/codex"),
            codex_home_base=self._tmp,
            credential_supplier=_credentials,
            home_factory=lambda base, account: Path(base) / account,
            process_factory=self._process,
            idle_grace_s=3600.0,
        )
        await self.manager.ensure(ACCOUNT)
        self.server = self.processes[0].server
        self.server.rule(schema.METHOD_THREAD_START, result=thread_config())
        self.server.rule(schema.METHOD_THREAD_RESUME, result=thread_config())
        self.server.rule(schema.METHOD_THREAD_READ, result={"thread": thread_payload()})
        self.server.rule(schema.METHOD_THREAD_ARCHIVE, result={})
        self.server.rule(schema.METHOD_TURN_START, result={"turn": turn_payload("turn-1")})
        self.server.rule(schema.METHOD_TURN_STEER, result={"turnId": "turn-1"})
        self.server.rule(schema.METHOD_TURN_INTERRUPT, result={})
        self.server.rule(
            schema.METHOD_TURN_SETTINGS_UPDATE, result={"status": "applied"}
        )

        async def _on_engine_session_id(session_id: str, thread_id: str) -> None:
            self.engine_ids.append((session_id, thread_id))

        async def _on_status(session_id: str, status: str) -> None:
            self.statuses.append(status)

        async def _on_context_usage(session_id: str, used: int, mx: int) -> None:
            self.context_usage.append((used, mx))

        async def _on_recoverable_failure(
            session_id: str, kind: str, retry_after: float | None
        ) -> None:
            self.failures.append(kind)

        self.driver = CodexAgentDriver(
            SESSION,
            ACCOUNT,
            cwd or str(self._tmp / "work"),
            str(self._tmp / "cfg"),
            mode=mode,
            manager=self.manager,
            registry=self.registry,
            identity="user@example.com",
            resume=resume,
            model=model,
            on_engine_session_id=_on_engine_session_id,
            on_status=_on_status,
            on_context_usage=_on_context_usage,
            on_recoverable_failure=_on_recoverable_failure,
        )
        self._collector = asyncio.create_task(self._collect())
        await asyncio.sleep(0)
        if start:
            await self.driver.start()
            await self.settle()
        return self.driver

    async def _collect(self) -> None:
        async for event in self.driver.events():
            self.events.append(event)

    async def settle(self) -> None:
        """Let the client read everything sent and the driver's pump digest it."""
        await self.server.drained()
        for _ in range(4):
            for _ in range(30):
                await asyncio.sleep(0)
            await asyncio.sleep(0.005)

    def of(self, kind: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]

    def frames(self, method: str) -> list[dict[str, Any]]:
        return self.server.frames_for(method)

    def params(self, method: str, index: int = 0) -> dict[str, Any]:
        return self.frames(method)[index]["params"]

    # -- server -> client helpers -----------------------------------------

    def notify_turn_started(self, turn_id: str = "turn-1") -> None:
        self.server.notify(
            schema.METHOD_TURN_STARTED,
            {"threadId": THREAD, "turn": turn_payload(turn_id)},
        )

    def notify_text(self, text: str, turn_id: str = "turn-1") -> None:
        self.server.notify(
            schema.METHOD_ITEM_AGENT_MESSAGE_DELTA,
            {"threadId": THREAD, "turnId": turn_id, "itemId": "item-1", "delta": text},
        )

    def notify_turn_completed(
        self, turn_id: str = "turn-1", status: str = "completed"
    ) -> None:
        self.server.notify(
            schema.METHOD_TURN_COMPLETED,
            {"threadId": THREAD, "turn": turn_payload(turn_id, status)},
        )

    async def run_turn(self, text: str = "hello", turn_id: str = "turn-1") -> None:
        """Send a prompt and let the app-server open its turn."""
        await self.driver.send_prompt(text)
        self.notify_turn_started(turn_id)
        await self.settle()

    async def aclose(self) -> None:
        await self.driver.aclose()
        if self._collector is not None:
            await asyncio.wait({self._collector}, timeout=1)
            self._collector.cancel()
        await self.manager.stop_all()


@pytest_asyncio.fixture
async def harness(tmp_path):
    h = Harness(tmp_path)
    try:
        yield h
    finally:
        if getattr(h, "driver", None) is not None:
            await h.aclose()


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_driver_satisfies_the_agent_driver_protocol(harness):
    """`AgentDriver` is @runtime_checkable, so a missing member is a failed
    isinstance rather than an AttributeError three weeks later in production."""
    driver = await harness.boot()
    assert isinstance(driver, AgentDriver)
    assert driver.engine is AgentEngine.CODEX
    caps = driver.capabilities
    assert (caps.models, caps.steer, caps.interrupt, caps.approvals) == (
        True, True, True, True,
    )
    assert not caps.questions and not caps.self_model_switch and not caps.fork


@pytest.mark.asyncio
async def test_start_opens_a_thread_with_the_mode_s_sandbox_and_markers(harness):
    driver = await harness.boot(mode="interactive")
    params = harness.params(schema.METHOD_THREAD_START)
    assert params["sandbox"] == "workspace-write"
    assert params["approvalPolicy"] == "untrusted"
    assert COMPLETION_MARKER in params["developerInstructions"]
    # Codex cannot act on a self-directed model request, so it is never asked for one.
    assert MODEL_REQUEST_INSTRUCTION not in params["developerInstructions"]
    assert driver.engine_session_id == THREAD
    assert harness.engine_ids == [(SESSION, THREAD)]


@pytest.mark.asyncio
async def test_plan_mode_starts_read_only(harness):
    """Plan mode's guarantee is enforced by the sandbox, not by the prompt."""
    await harness.boot(mode="plan")
    assert harness.params(schema.METHOD_THREAD_START)["sandbox"] == "read-only"


@pytest.mark.asyncio
async def test_start_resumes_an_existing_thread(harness):
    await harness.boot(resume=THREAD)
    assert not harness.frames(schema.METHOD_THREAD_START)
    assert harness.params(schema.METHOD_THREAD_RESUME)["threadId"] == THREAD


@pytest.mark.asyncio
async def test_start_fails_typed_when_the_runner_disappeared(harness):
    driver = await harness.boot(start=False)
    await harness.manager.stop(ACCOUNT)
    with pytest.raises(CodexRunnerUnavailable):
        await driver.start()


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_prompt_starts_a_turn_and_streams_its_answer(harness):
    driver = await harness.boot()
    await harness.run_turn("write a haiku")
    harness.notify_text("cherry blossoms fall")
    harness.notify_turn_completed()
    await harness.settle()

    assert harness.params(schema.METHOD_TURN_START)["input"] == [
        {"type": "text", "text": "write a haiku"}
    ]
    assert [e.text for e in harness.of(ev.UserMessage)] == ["write a haiku"]
    assert [e.text for e in harness.of(ev.TextDelta)] == ["cherry blossoms fall"]
    ends = harness.of(ev.TurnEnd)
    assert [e.outcome for e in ends] == ["success"]
    assert driver.turn_active is False
    assert driver.current_status() == "done"


@pytest.mark.asyncio
async def test_the_native_user_message_item_does_not_double_the_echo(harness):
    """The driver's own echo is the one that carries attachments and the route
    chip; forwarding the app-server's `userMessage` item too shows every prompt
    twice."""
    driver = await harness.boot()
    await harness.run_turn("hello")
    harness.server.notify(
        schema.METHOD_ITEM_COMPLETED,
        {
            "threadId": THREAD,
            "turnId": "turn-1",
            "completedAtMs": 1,
            "item": {"type": "userMessage", "id": "item-u", "content": []},
        },
    )
    await harness.settle()

    assert len(harness.of(ev.UserMessage)) == 1
    assert driver.counters.dropped_user_echoes == 1


@pytest.mark.asyncio
async def test_a_second_prompt_steers_the_live_turn(harness):
    driver = await harness.boot()
    await harness.run_turn("first")
    await driver.send_prompt("also check the tests")
    await harness.settle()

    steer = harness.params(schema.METHOD_TURN_STEER)
    assert steer["expectedTurnId"] == "turn-1"
    assert steer["input"] == [{"type": "text", "text": "also check the tests"}]
    # Steering is not queueing: the prompt joined the running turn.
    assert driver.queued() == []
    assert len(harness.frames(schema.METHOD_TURN_START)) == 1


@pytest.mark.asyncio
async def test_a_steer_that_lost_the_race_is_queued_and_drained_never_lost(harness):
    """`expectedTurnId` is what makes the race detectable; the prompt must then
    wait for the next turn rather than vanish."""
    driver = await harness.boot()
    await harness.run_turn("first")
    harness.server.rule(
        schema.METHOD_TURN_STEER,
        error={"code": -32600, "message": "activeTurnNotSteerable"},
    )
    await driver.send_prompt("follow-up")
    await harness.settle()
    assert driver.queued() == ["follow-up"]

    harness.notify_turn_completed()
    await harness.settle()

    starts = harness.frames(schema.METHOD_TURN_START)
    assert len(starts) == 2
    assert starts[1]["params"]["input"] == [{"type": "text", "text": "follow-up"}]
    assert driver.queued() == []
    assert driver.counters.steer_fallbacks == 1


@pytest.mark.asyncio
async def test_interrupt_stops_the_turn_and_drops_the_queue(harness):
    driver = await harness.boot()
    await harness.run_turn("first")
    harness.server.rule(
        schema.METHOD_TURN_STEER, error={"code": -32600, "message": "nope"}
    )
    await driver.send_prompt("queued")
    await harness.settle()

    await driver.interrupt()
    await harness.settle()

    assert harness.params(schema.METHOD_TURN_INTERRUPT) == {
        "threadId": THREAD, "turnId": "turn-1",
    }
    assert driver.turn_active is False
    assert driver.queued() == []
    assert len(harness.frames(schema.METHOD_TURN_START)) == 1


@pytest.mark.asyncio
async def test_interrupt_and_post_runs_the_new_prompt_ahead_of_the_queue(harness):
    driver = await harness.boot()
    await harness.run_turn("first")
    harness.server.rule(
        schema.METHOD_TURN_STEER, error={"code": -32600, "message": "nope"}
    )
    await driver.send_prompt("was queued")
    await harness.settle()

    await driver.send_prompt("urgent", interrupt=True)
    await harness.settle()

    starts = harness.frames(schema.METHOD_TURN_START)
    assert len(starts) == 2
    assert starts[1]["params"]["input"] == [{"type": "text", "text": "urgent"}]
    assert driver.queued() == ["was queued"]


@pytest.mark.asyncio
async def test_a_late_completion_for_an_interrupted_turn_never_ends_the_next_one(
    harness,
):
    """The interrupted turn's `turn/completed` can arrive after the next turn has
    started; ending the live turn on it would kill running work."""
    driver = await harness.boot()
    await harness.run_turn("first")
    await driver.interrupt(keep_queue=True)
    harness.server.rule(
        schema.METHOD_TURN_START, result={"turn": turn_payload("turn-2")}
    )
    await driver.send_prompt("second")
    harness.notify_turn_started("turn-2")
    await harness.settle()

    harness.notify_turn_completed("turn-1", status="interrupted")
    await harness.settle()

    assert driver.turn_active is True
    assert [e.turn_id for e in harness.of(ev.TurnEnd)] == ["turn-1"]


# ---------------------------------------------------------------------------
# The crash fence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_whose_runner_was_replaced_is_never_reported_successful(harness):
    """`finish_turn` — not the app-server's own `completed` — decides. The
    process that ran the turn is gone, so its outcome is unknowable."""
    driver = await harness.boot()
    await harness.run_turn("do the thing")
    # A replacement runner was minted while the turn was in flight.
    harness.manager._generations[ACCOUNT] = 99
    harness.notify_turn_completed()
    await harness.settle()

    assert [e.outcome for e in harness.of(ev.TurnEnd)] == ["interrupted"]
    assert driver.current_status() == "needs_attention"


@pytest.mark.asyncio
async def test_a_crash_notice_mid_turn_interrupts_it(harness):
    driver = await harness.boot()
    await harness.run_turn("do the thing")

    driver._on_notice(
        RunnerNotice(
            kind=RunnerNoticeKind.CRASHED, account=ACCOUNT, generation=1,
            interrupted_turns=("turn-1",),
        )
    )
    await harness.settle()

    assert [e.outcome for e in harness.of(ev.TurnEnd)] == ["interrupted"]
    assert [e.code for e in harness.of(ev.ErrorEvent)] == ["codex_runner_crashed"]
    assert driver.turn_active is False
    assert driver.current_status() == "needs_attention"


@pytest.mark.asyncio
async def test_a_restart_notice_re_reads_the_thread(harness):
    driver = await harness.boot()
    driver._on_notice(
        RunnerNotice(
            kind=RunnerNoticeKind.RESTARTED, account=ACCOUNT, generation=7,
            recover_via=RECOVER_VIA_THREAD_READ,
        )
    )
    await harness.settle()

    assert harness.params(schema.METHOD_THREAD_READ)["threadId"] == THREAD
    assert harness.of(ev.ErrorEvent) == []


@pytest.mark.asyncio
async def test_a_thread_that_cannot_be_re_read_is_surfaced_for_recovery(harness):
    driver = await harness.boot()
    harness.server.rule(
        schema.METHOD_THREAD_READ, error={"code": -32000, "message": "no such thread"}
    )
    driver._on_notice(
        RunnerNotice(
            kind=RunnerNoticeKind.RESTARTED, account=ACCOUNT, generation=7,
            recover_via=RECOVER_VIA_THREAD_READ,
        )
    )
    await harness.settle()

    assert [e.code for e in harness.of(ev.ErrorEvent)] == ["codex_thread_lost"]
    assert harness.failures == ["process_crash"]


@pytest.mark.asyncio
async def test_an_unrecoverable_runner_is_surfaced_and_signalled(harness):
    driver = await harness.boot()
    driver._on_notice(
        RunnerNotice(
            kind=RunnerNoticeKind.UNRECOVERABLE, account=ACCOUNT, generation=1
        )
    )
    await harness.settle()

    assert [e.code for e in harness.of(ev.ErrorEvent)] == ["codex_runner_unrecoverable"]
    assert harness.failures == ["process_crash"]


# ---------------------------------------------------------------------------
# Status, usage, errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_status_never_overwrites_the_derived_status(harness):
    """The translator speaks working/idle; ccswitch's dashboard speaks a richer
    vocabulary. Forwarding the raw status would paint 'idle' over a live turn."""
    driver = await harness.boot()
    await harness.run_turn("hello")
    harness.server.notify(
        schema.METHOD_THREAD_STATUS_CHANGED,
        {"threadId": THREAD, "status": {"type": "idle"}},
    )
    await harness.settle()

    assert {e.state for e in harness.of(ev.Status)} <= {"in_progress", "idle", "done"}
    assert harness.of(ev.Status)[-1].state == "in_progress"
    assert driver.turn_active is True


@pytest.mark.asyncio
async def test_context_usage_is_forwarded_and_persisted(harness):
    driver = await harness.boot()
    await harness.run_turn("hello")
    breakdown = {
        "inputTokens": 10, "cachedInputTokens": 0, "outputTokens": 5,
        "reasoningOutputTokens": 0, "totalTokens": 15,
    }
    harness.server.notify(
        schema.METHOD_THREAD_TOKEN_USAGE_UPDATED,
        {
            "threadId": THREAD,
            "turnId": "turn-1",
            "tokenUsage": {
                "total": breakdown, "last": breakdown, "modelContextWindow": 1500,
            },
        },
    )
    await harness.settle()

    assert harness.context_usage == [(15, 1500)]
    assert (await driver.context_usage())["maxTokens"] == 1500


@pytest.mark.asyncio
async def test_an_unauthorized_error_dismisses_prompts_and_signals_recovery(harness):
    """Auth loss is an exit path: a human's answer can no longer be honoured, so
    nothing may sit waiting on one."""
    driver = await harness.boot()
    await harness.run_turn("hello")
    harness.server.notify(
        "error",
        {
            "threadId": THREAD,
            "turnId": "turn-1",
            "willRetry": False,
            "error": {"message": "nope", "codexErrorInfo": "unauthorized"},
        },
    )
    await harness.settle()

    assert [e.code for e in harness.of(ev.ErrorEvent)] == ["unauthorized"]
    # The native message is Redacted upstream and never reaches a client.
    assert "nope" not in harness.of(ev.ErrorEvent)[0].message
    assert harness.failures == ["auth_401"]
    assert [e.kind for e in harness.of(ev.TurnFailure)] == ["auth_401"]
    assert driver.counters.untranslated_notifications == 1


# ---------------------------------------------------------------------------
# Model / permission mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_model_mid_turn_retargets_the_live_turn(harness):
    driver = await harness.boot()
    await harness.run_turn("hello")
    await driver.set_model("gpt-5.1-codex-max")
    await harness.settle()

    assert harness.params(schema.METHOD_TURN_SETTINGS_UPDATE) == {
        "threadId": THREAD, "turnId": "turn-1", "model": "gpt-5.1-codex-max",
    }


@pytest.mark.asyncio
async def test_set_model_between_turns_applies_to_the_next_turn(harness):
    driver = await harness.boot()
    await driver.set_model("gpt-5.1-codex-max")
    await driver.send_prompt("hello")
    await harness.settle()

    assert not harness.frames(schema.METHOD_TURN_SETTINGS_UPDATE)
    assert harness.params(schema.METHOD_TURN_START)["model"] == "gpt-5.1-codex-max"


@pytest.mark.asyncio
async def test_the_default_model_sentinel_means_the_account_default(harness):
    driver = await harness.boot(model="gpt-5.1-codex-max")
    await driver.set_model("default")
    await driver.send_prompt("hello")
    await harness.settle()

    assert "model" not in harness.params(schema.METHOD_TURN_START)


@pytest.mark.asyncio
async def test_an_unsupported_permission_mode_is_refused_not_defaulted(harness):
    """`set_permission_mode` is unvalidated end to end, so an arbitrary string
    genuinely arrives here. Defaulting is how a session ends up with authority
    nobody granted."""
    driver = await harness.boot(mode="interactive")
    await driver.set_permission_mode("yolo")
    await harness.settle()

    assert driver.mode == "interactive"
    assert [e.code for e in harness.of(ev.ErrorEvent)] == [
        "codex_permission_mode_refused"
    ]
    assert not harness.frames(schema.METHOD_THREAD_RESUME)


@pytest.mark.asyncio
async def test_a_narrower_sandbox_is_applied_by_re_resuming_the_thread(harness):
    driver = await harness.boot(mode="interactive")
    await driver.set_permission_mode("plan")
    await harness.settle()

    assert harness.params(schema.METHOD_THREAD_RESUME)["sandbox"] == "read-only"
    assert driver.mode == "plan"


@pytest.mark.asyncio
async def test_a_sandbox_change_that_fails_refuses_the_switch(harness):
    """Never silently widen: if the sandbox could not be changed, the mode must
    not move either, or the new approval policy applies under the old sandbox."""
    driver = await harness.boot(mode="interactive")
    harness.server.rule(
        schema.METHOD_THREAD_RESUME, error={"code": -32000, "message": "denied"}
    )
    await driver.set_permission_mode("bypassPermissions")
    await harness.settle()

    assert driver.mode == "interactive"
    assert [e.code for e in harness.of(ev.ErrorEvent)] == [
        "codex_sandbox_change_refused"
    ]


@pytest.mark.asyncio
async def test_a_sandbox_change_waits_for_the_next_turn_boundary(harness):
    """The sandbox is immutable mid-thread, and changing it under a running turn
    would move the ground beneath work already in flight."""
    driver = await harness.boot(mode="interactive")
    await harness.run_turn("hello")
    await driver.set_permission_mode("plan")
    await harness.settle()
    assert not harness.frames(schema.METHOD_THREAD_RESUME)
    assert driver.mode == "interactive"

    harness.notify_turn_completed()
    await harness.settle()
    await driver.send_prompt("next")
    await harness.settle()

    assert harness.params(schema.METHOD_THREAD_RESUME)["sandbox"] == "read-only"
    assert driver.mode == "plan"


@pytest.mark.asyncio
async def test_the_plan_toggle_s_default_sentinel_restores_the_previous_mode(harness):
    driver = await harness.boot(mode="full-auto")
    await driver.set_permission_mode("plan")
    await harness.settle()
    await driver.set_permission_mode("default")
    await harness.settle()

    assert driver.mode == "full-auto"
    assert [
        f["params"]["sandbox"] for f in harness.frames(schema.METHOD_THREAD_RESUME)
    ] == ["read-only", "workspace-write"]


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_image_inside_the_workspace_is_sent_as_a_local_image(harness, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    shot = work / "shot.png"
    shot.write_bytes(b"png")
    driver = await harness.boot(cwd=str(work))
    await driver.send_prompt(
        "look", images=[{"path": str(shot), "mime": "image/png", "url": "/u", "name": "shot.png"}]
    )
    await harness.settle()

    assert harness.params(schema.METHOD_TURN_START)["input"] == [
        {"type": "text", "text": "look"},
        {"type": "localImage", "path": str(shot.resolve())},
    ]


@pytest.mark.asyncio
async def test_an_attachment_outside_the_workspace_is_refused(harness, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"png")
    driver = await harness.boot(cwd=str(work))
    with pytest.raises(CodexAttachmentRejected):
        await driver.send_prompt(
            "look", images=[{"path": str(outside), "mime": "image/png"}]
        )

    assert not harness.frames(schema.METHOD_TURN_START)
    assert driver.turn_active is False


@pytest.mark.asyncio
async def test_a_symlink_out_of_the_workspace_is_refused(harness, tmp_path):
    """Containment is checked against the REAL path: a symlink planted inside
    the workspace must not smuggle a file from outside it into the context."""
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"png")
    link = work / "innocent.png"
    link.symlink_to(outside)
    driver = await harness.boot(cwd=str(work))
    with pytest.raises(CodexAttachmentRejected):
        await driver.send_prompt("look", images=[{"path": str(link), "mime": "image/png"}])


@pytest.mark.asyncio
async def test_a_non_image_attachment_is_refused_rather_than_dropped(harness, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    doc = work / "spec.pdf"
    doc.write_bytes(b"%PDF")
    driver = await harness.boot(cwd=str(work))
    with pytest.raises(CodexAttachmentRejected):
        await driver.send_prompt(
            "read this", images=[{"path": str(doc), "mime": "application/pdf"}]
        )


# ---------------------------------------------------------------------------
# Queue / stream / teardown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_queued_prompt_can_be_cancelled(harness):
    driver = await harness.boot()
    await harness.run_turn("first")
    harness.server.rule(
        schema.METHOD_TURN_STEER, error={"code": -32600, "message": "nope"}
    )
    await driver.send_prompt("second")
    await harness.settle()

    assert driver.queue_wire() == [{"text": "second", "images": []}]
    assert await driver.cancel_queued(0) is True
    assert await driver.cancel_queued(0) is False
    assert driver.queued() == []


@pytest.mark.asyncio
async def test_a_late_subscriber_replays_the_turn_in_progress(harness):
    driver = await harness.boot()
    await harness.run_turn("hello")
    harness.notify_text("partial answer")
    await harness.settle()

    stream = driver.open_stream()
    try:
        assert stream.turn_active is True
        replayed = list(stream._snapshot)
    finally:
        stream.close()

    assert [e.text for e in replayed if isinstance(e, ev.TextDelta)] == [
        "partial answer"
    ]
    assert any(isinstance(e, ev.UserMessage) for e in replayed)


@pytest.mark.asyncio
async def test_reconnect_starts_a_fresh_thread(harness):
    driver = await harness.boot()
    await harness.run_turn("hello")
    harness.server.rule(
        schema.METHOD_THREAD_START, result=thread_config("thread-2")
    )
    await driver.reconnect()
    await harness.settle()

    assert driver.engine_session_id == "thread-2"
    assert driver.turn_active is False
    assert len(harness.frames(schema.METHOD_THREAD_START)) == 2


@pytest.mark.asyncio
async def test_aclose_unregisters_the_thread_and_ends_the_stream(harness):
    driver = await harness.boot()
    await driver.aclose()
    await harness.settle()

    # The manager no longer routes to a closed driver, so a stray frame takes
    # the unrouted path rather than reaching a driver that is shutting down.
    harness.manager._on_notification(
        ACCOUNT,
        schema.METHOD_TURN_STARTED,
        schema.parse_server_notification(
            schema.METHOD_TURN_STARTED,
            {"threadId": THREAD, "turn": turn_payload("turn-9")},
        ),
    )
    assert harness.manager.unrouted_events == 1
    assert harness._collector is not None and harness._collector.done()


@pytest.mark.asyncio
async def test_archive_archives_the_thread_then_closes(harness):
    driver = await harness.boot()
    await driver.archive()
    await harness.settle()

    assert harness.params(schema.METHOD_THREAD_ARCHIVE)["threadId"] == THREAD


@pytest.mark.asyncio
async def test_surfaces_codex_does_not_have_answer_honestly(harness):
    driver = await harness.boot()
    assert await driver.server_info() is None
    assert driver.pending_questions() == []
    assert driver.answer_question("q", ["a"], "user@example.com", "option") is False
    assert driver.seed_idle_suggestions("u", "a") is None
    assert driver.in_api_backoff is False


@pytest.mark.asyncio
async def test_the_factory_builds_a_driver_from_the_shared_config(harness):
    await harness.boot()
    factory = CodexAgentDriverFactory(
        harness.manager, harness.registry, model_for_session=lambda sid: "gpt-5.1-codex"
    )
    built = factory.create(
        AgentDriverConfig(
            session_id="sess-2",
            account=ACCOUNT,
            cwd="/work",
            config_dir="/cfg",
            mode="interactive",
            identity="user@example.com",
            reasoning_effort="high",
        )
    )
    assert isinstance(built, CodexAgentDriver)
    assert built.engine is AgentEngine.CODEX
    assert built._model == "gpt-5.1-codex"
    assert built._effort == "high"
