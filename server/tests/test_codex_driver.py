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
import hashlib
import subprocess
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from ccswitch import paths as ccswitch_paths
from server.codex import schema
from server.codex.capabilities import CodexAvailability
from server.codex.driver import (
    CodexAgentDriver,
    CodexAgentDriverFactory,
    CodexAttachmentRejected,
    CodexPolicyTransitionBlocked,
    CodexRunnerUnavailable,
    CodexThreadNotStarted,
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
        assistant_overlay: Any = None,
        session_tools: Any = None,
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
        def configured_thread(params):
            native_sandbox = {
                "read-only": "readOnly", "workspace-write": "workspaceWrite",
                "danger-full-access": "dangerFullAccess",
            }[params["sandbox"]]
            return thread_config(sandbox=native_sandbox,
                                 approval_policy=params["approvalPolicy"])

        self.server.rule(schema.METHOD_THREAD_START, result=configured_thread)
        self.server.rule(schema.METHOD_THREAD_RESUME, result=configured_thread)
        self.server.rule(schema.METHOD_THREAD_READ, result={"thread": thread_payload()})
        self.server.rule(schema.METHOD_THREAD_BACKGROUND_TERMINALS_LIST,
                         result={"data": [], "nextCursor": None})
        self.server.rule(schema.METHOD_THREAD_GOAL_GET, result={"goal": None})
        self.server.rule(schema.METHOD_THREAD_FORK,
                         error={"code": -32601, "message": "fork denied"})
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
            assistant_overlay=assistant_overlay,
            session_tools=session_tools,
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
    assert caps.questions
    assert not caps.self_model_switch and not caps.fork


@pytest.mark.asyncio
async def test_full_auto_starts_with_unattended_filesystem_and_network_access(harness):
    """Full-auto gives routine work filesystem/network authority without cards."""
    await harness.boot(mode="full-auto")
    params = harness.params(schema.METHOD_THREAD_START)
    assert params["sandbox"] == "danger-full-access"
    assert params["approvalPolicy"] == "never"


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
async def test_codex_emit_status_broadcasts_overlaid_on_status_result(harness):
    """The live Status event carries the value on_status RETURNS (the orchestrator's
    overlay), mirroring ClaudeAgentDriver — so the in-session StatusBar and the
    session list can never disagree, on either engine."""
    driver = await harness.boot()

    async def on_status(session_id, status):
        return "waiting_question" if status == "done" else status

    driver._on_status = on_status
    await harness.run_turn("write a haiku")
    harness.notify_text("cherry blossoms fall")
    harness.notify_turn_completed()
    await harness.settle()

    assert driver.current_status() == "waiting_question"
    assert harness.of(ev.Status)[-1].state == "waiting_question"


@pytest.mark.asyncio
async def test_codex_seed_status_adopts_persisted_resting_status(harness):
    """A rebuilt driver (daemon restart / completion release) derives `idle` from its
    fresh signals; seeding the persisted status keeps the attach snapshot consistent
    with the session card instead of flashing a false "Ready"."""
    driver = await harness.boot(start=False)
    driver.seed_status("waiting_question")
    assert driver.current_status() == "waiting_question"
    driver._turn_active = True   # first real turn supersedes the seed
    assert driver.current_status() == "in_progress"


@pytest.mark.asyncio
async def test_codex_seed_status_ignores_transient_statuses(harness):
    """Mid-turn statuses can't be resurrected on a fresh driver — never seed them."""
    driver = await harness.boot(start=False)
    for transient in ("in_progress", "condensing", "retrying", "queued", ""):
        driver.seed_status(transient)
        assert driver.current_status() == "idle"


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
    ] == ["read-only", "danger-full-access"]


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


@pytest.mark.asyncio
async def test_a_global_store_image_is_staged_into_the_workspace(
    harness, tmp_path, monkeypatch
):
    """#1291 — a normal upload lives in the content-addressed global store,
    outside cwd. Codex only accepts a `localImage` inside realpath(cwd), so the
    driver must stage a copy rather than reject a completely ordinary upload."""
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(ccswitch_paths, "attachments_dir", lambda: store)
    payload = b"png-bytes"
    upload = store / f"{hashlib.sha256(payload).hexdigest()}.png"
    upload.write_bytes(payload)
    work = tmp_path / "work"
    work.mkdir()
    driver = await harness.boot(cwd=str(work))

    await driver.send_prompt(
        "look", images=[{"path": str(upload), "mime": "image/png", "name": "shot.png"}]
    )
    await harness.settle()

    staged_path = harness.params(schema.METHOD_TURN_START)["input"][1]["path"]
    staged = Path(staged_path)
    assert staged.is_relative_to(work.resolve())
    assert staged.read_bytes() == payload
    assert upload.exists()  # the original upload is left in place


@pytest.mark.asyncio
async def test_staging_a_resent_image_reuses_the_same_file(
    harness, tmp_path, monkeypatch
):
    """Content-addressed: resending the same bytes must not grow the staging
    dir unbounded across turns."""
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(ccswitch_paths, "attachments_dir", lambda: store)
    payload = b"png-bytes"
    upload = store / f"{hashlib.sha256(payload).hexdigest()}.png"
    upload.write_bytes(payload)
    work = tmp_path / "work"
    work.mkdir()
    driver = await harness.boot(cwd=str(work))

    await harness.run_turn("first")
    await driver.send_prompt(
        "look", images=[{"path": str(upload), "mime": "image/png", "name": "shot.png"}]
    )
    harness.notify_turn_completed()
    await harness.settle()
    await driver.send_prompt(
        "look again",
        images=[{"path": str(upload), "mime": "image/png", "name": "shot.png"}],
    )
    await harness.settle()

    staged_paths = {
        f["params"]["input"][1]["path"]
        for f in harness.frames(schema.METHOD_TURN_START)
        if len(f["params"]["input"]) > 1
    }
    assert len(staged_paths) == 1
    staging_dir = work / ccswitch_paths.codex_attachment_staging_dirname()
    assert len(list(staging_dir.iterdir())) == 1


@pytest.mark.asyncio
async def test_staging_registers_a_git_exclude_entry(harness, tmp_path, monkeypatch):
    """The staging dir must never surface in `git status` for the user's repo
    — otherwise it reads as uncommitted work and blocks worktree cleanup
    (src/ccswitch/worktrees.py:remove_worktree)."""
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(ccswitch_paths, "attachments_dir", lambda: store)
    payload = b"png-bytes"
    upload = store / f"{hashlib.sha256(payload).hexdigest()}.png"
    upload.write_bytes(payload)
    work = tmp_path / "work"
    work.mkdir()
    subprocess.run(["git", "init", str(work)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "Test"], check=True)
    driver = await harness.boot(cwd=str(work))

    await driver.send_prompt(
        "look", images=[{"path": str(upload), "mime": "image/png", "name": "shot.png"}]
    )
    await harness.settle()

    exclude = (work / ".git" / "info" / "exclude").read_text()
    assert f"/{ccswitch_paths.codex_attachment_staging_dirname()}/" in exclude.splitlines()
    status = subprocess.run(
        ["git", "-C", str(work), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    )
    assert status.stdout.strip() == ""


@pytest.mark.asyncio
async def test_a_symlinked_attachment_pretending_to_be_from_the_store_is_refused(
    harness, tmp_path, monkeypatch
):
    """The store-membership check must resolve the REAL path — a symlink
    planted inside the store must not smuggle an outside file through staging."""
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setattr(ccswitch_paths, "attachments_dir", lambda: store)
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"secret")
    link = store / "innocent.png"
    link.symlink_to(outside)
    work = tmp_path / "work"
    work.mkdir()
    driver = await harness.boot(cwd=str(work))

    with pytest.raises(CodexAttachmentRejected):
        await driver.send_prompt(
            "look", images=[{"path": str(link), "mime": "image/png"}]
        )


@pytest.mark.asyncio
async def test_a_text_attachment_is_inlined_rather_than_refused(harness, tmp_path):
    """#1276 — Codex has no document modality on the wire, but a plain-text
    file can be decoded and folded into the turn's text input, same as Claude
    does for the identical mime type."""
    work = tmp_path / "work"
    work.mkdir()
    doc = work / "notes.txt"
    doc.write_text("hello from a text attachment")
    driver = await harness.boot(cwd=str(work))

    await driver.send_prompt(
        "read this", images=[{"path": str(doc), "mime": "text/plain", "name": "notes.txt"}]
    )
    await harness.settle()

    assert harness.params(schema.METHOD_TURN_START)["input"] == [
        {"type": "text", "text": "read this"},
        {"type": "text", "text": "hello from a text attachment"},
    ]


@pytest.mark.asyncio
async def test_a_csv_and_json_attachment_are_inlined_too(harness, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    csv = work / "data.csv"
    csv.write_text("a,b\n1,2")
    js = work / "data.json"
    js.write_text('{"a": 1}')
    driver = await harness.boot(cwd=str(work))

    await driver.send_prompt(
        "read these",
        images=[
            {"path": str(csv), "mime": "text/csv", "name": "data.csv"},
            {"path": str(js), "mime": "application/json", "name": "data.json"},
        ],
    )
    await harness.settle()

    assert harness.params(schema.METHOD_TURN_START)["input"] == [
        {"type": "text", "text": "read these"},
        {"type": "text", "text": "a,b\n1,2"},
        {"type": "text", "text": '{"a": 1}'},
    ]


@pytest.mark.asyncio
async def test_a_zip_attachment_is_described_not_read(harness, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    archive = work / "bundle.zip"
    archive.write_bytes(b"PK\x03\x04zip-bytes")
    driver = await harness.boot(cwd=str(work))

    await driver.send_prompt(
        "what's in this",
        images=[{"path": str(archive), "mime": "application/zip", "name": "bundle.zip"}],
    )
    await harness.settle()

    assert harness.params(schema.METHOD_TURN_START)["input"] == [
        {"type": "text", "text": "what's in this"},
        {"type": "text", "text": "[Attached: bundle.zip (zip archive, 13 bytes)]"},
    ]


@pytest.mark.asyncio
async def test_a_text_attachment_outside_both_roots_is_still_refused(
    harness, tmp_path
):
    """Extending non-image support must not weaken containment: a text file
    is read into the prompt, so leaking an arbitrary host path here is worse
    than the image case, not equivalent."""
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "host-secret.txt"
    outside.write_text("do not leak me")
    driver = await harness.boot(cwd=str(work))

    with pytest.raises(CodexAttachmentRejected):
        await driver.send_prompt(
            "read this", images=[{"path": str(outside), "mime": "text/plain"}]
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
async def test_a_stale_queued_attachment_does_not_strand_later_prompts(
    harness, tmp_path
):
    work = tmp_path / "work"
    work.mkdir()
    shot = work / "shot.png"
    shot.write_bytes(b"png")
    driver = await harness.boot(cwd=str(work))
    await harness.run_turn("first")
    harness.server.rule(
        schema.METHOD_TURN_STEER, error={"code": -32600, "message": "queue"}
    )
    await driver.send_prompt(
        "stale image", images=[{"path": str(shot), "mime": "image/png"}]
    )
    harness.server.rule(
        schema.METHOD_TURN_STEER, error={"code": -32600, "message": "queue"}
    )
    await driver.send_prompt("valid prompt")
    await harness.settle()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png")
    shot.unlink()
    shot.symlink_to(outside)

    harness.notify_turn_completed()
    await harness.settle()

    starts = harness.frames(schema.METHOD_TURN_START)
    assert starts[-1]["params"]["input"] == [
        {"type": "text", "text": "valid prompt"}
    ]
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
    old_pump = driver._pump
    await harness.run_turn("hello")
    harness.server.rule(
        schema.METHOD_THREAD_START, result=thread_config("thread-2")
    )
    await driver.reconnect()
    await harness.settle()

    assert driver.engine_session_id == "thread-2"
    assert driver.turn_active is False
    assert len(harness.frames(schema.METHOD_THREAD_START)) == 2
    assert old_pump is not None and old_pump.done()
    assert driver._pump is not old_pump


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
    """server_info() genuinely has nothing to report here (no skills registered
    on the fake app-server). seed_idle_suggestions is a deliberate no-op at the
    driver level (#1281) — Codex suggestions are generated centrally by
    SessionOrchestrator instead, see server/tests/test_suggestions_codex.py."""
    driver = await harness.boot()
    assert await driver.server_info() is None
    assert driver.seed_idle_suggestions("u", "a") is None
    assert driver.in_api_backoff is False


@pytest.mark.asyncio
async def test_answer_question_on_an_unknown_id_reports_not_pending(harness):
    """No question ever raised — the same "no longer pending" honesty the
    driver reports for any id the registry does not recognize."""
    driver = await harness.boot()
    assert driver.pending_questions() == []
    assert driver.answer_question("q", ["a"], "user@example.com", "option") is False


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


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [None, THREAD])
@pytest.mark.parametrize("mismatch", ["sandbox", "approval"])
async def test_native_policy_mismatch_prevents_start(harness, resume, mismatch):
    driver = await harness.boot(mode="full-auto", resume=resume, start=False)
    harness.server.rule(
        schema.METHOD_THREAD_RESUME if resume else schema.METHOD_THREAD_START,
        result=thread_config(
            sandbox="workspaceWrite" if mismatch == "sandbox" else "dangerFullAccess",
            approval_policy="on-request" if mismatch == "approval" else "never",
        ),
    )
    with pytest.raises(CodexRunnerUnavailable, match="permission policy"):
        await driver.start()
    assert harness.engine_ids == []
    assert harness.of(ev.SessionMeta) == []
    with pytest.raises(CodexThreadNotStarted):
        await driver.send_prompt("must not run")
    assert harness.frames(schema.METHOD_TURN_START) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_sandbox,saved_approval", [
    ("workspaceWrite", "on-request"),
    ("readOnly", "untrusted"),
])
async def test_rebuild_reconciles_saved_native_policy_with_durable_full_auto(
    harness, saved_sandbox, saved_approval,
):
    # The row stores the selected base mode, while a loaded thread may retain a
    # legacy full-auto policy or the transient plan overlay across driver loss.
    original = await harness.boot(mode="full-auto")
    if saved_sandbox == "readOnly":
        await original.set_permission_mode("plan")
        assert original.mode == "plan"
    await original.aclose()
    await harness.manager.stop_all()
    harness.processes.clear()

    rebuilt = await harness.boot(mode="full-auto", resume=THREAD, start=False)
    harness.server.rule(schema.METHOD_THREAD_RESUME, result=thread_config(
        sandbox=saved_sandbox, approval_policy=saved_approval,
    ))
    harness.server.rule(schema.METHOD_THREAD_FORK, result=thread_config(
        "rebuilt-full-auto", sandbox="dangerFullAccess", approval_policy="never",
    ))
    await rebuilt.start()
    await harness.settle()
    assert rebuilt.mode == "full-auto"
    assert harness.engine_ids[-1] == (SESSION, "rebuilt-full-auto")
    assert harness.of(ev.SessionMeta)[-1].permission_mode == "full-auto"
    await rebuilt.send_prompt("continue after rebuild")
    assert harness.params(schema.METHOD_TURN_START)["threadId"] == "rebuilt-full-auto"
    assert harness.params(schema.METHOD_TURN_START)["approvalPolicy"] == "never"
    assert harness.params(schema.METHOD_THREAD_FORK)["threadId"] == THREAD
    assert not harness.frames(schema.METHOD_THREAD_START)


@pytest.mark.asyncio
async def test_rebuild_policy_reconciliation_preserves_native_goal_with_explicit_error(harness):
    driver = await harness.boot(mode="full-auto", resume=THREAD, start=False)
    harness.server.rule(schema.METHOD_THREAD_RESUME, result=thread_config(sandbox="readOnly"))
    harness.server.rule(schema.METHOD_THREAD_GOAL_GET, result={"goal": {
        "threadId": THREAD, "objective": "Keep working", "status": "active",
        "tokenBudget": 10000, "tokensUsed": 42, "timeUsedSeconds": 3,
        "createdAt": 1, "updatedAt": 2,
    }})
    with pytest.raises(CodexPolicyTransitionBlocked, match="native goal"):
        await driver.start()
    assert harness.engine_ids == []
    assert not harness.frames(schema.METHOD_THREAD_FORK)
    assert not harness.frames(schema.METHOD_THREAD_START)
    with pytest.raises(CodexThreadNotStarted):
        await driver.send_prompt("must not use a mismatched native policy")
    assert not harness.frames(schema.METHOD_TURN_START)


@pytest.mark.asyncio
async def test_runner_recovery_reconciles_saved_native_policy_and_updates_resume_handle(harness):
    driver = await harness.boot(mode="full-auto", resume=THREAD)
    harness.server.rule(schema.METHOD_THREAD_RESUME, result=thread_config(sandbox="readOnly"))
    harness.server.rule(schema.METHOD_THREAD_FORK, result=thread_config(
        "recovered-full-auto", sandbox="dangerFullAccess", approval_policy="never",
    ))
    driver._on_notice(RunnerNotice(
        kind=RunnerNoticeKind.RESTARTED, account=ACCOUNT, generation=7,
        recover_via=RECOVER_VIA_THREAD_READ,
    ))
    await harness.settle()
    assert harness.engine_ids[-1] == (SESSION, "recovered-full-auto")
    assert harness.failures == []
    await driver.send_prompt("continue after runner recovery")
    assert harness.params(schema.METHOD_TURN_START)["threadId"] == "recovered-full-auto"
    assert harness.params(schema.METHOD_TURN_START)["approvalPolicy"] == "never"


@pytest.mark.asyncio
async def test_native_mismatch_on_plan_exit_blocks_execution_and_allows_retry(harness):
    driver = await harness.boot(mode="full-auto")
    await driver.set_permission_mode("plan")
    harness.server.rule(schema.METHOD_THREAD_RESUME,
                        result=thread_config(sandbox="readOnly"))
    await driver.set_permission_mode("default")
    await harness.settle()
    assert driver.mode == "plan"
    assert driver.current_status() == "needs_attention"
    with pytest.raises(CodexRunnerUnavailable, match="permission policy"):
        await driver.send_prompt("must not run")
    assert harness.frames(schema.METHOD_TURN_START) == []
    harness.server.rule(schema.METHOD_THREAD_RESUME,
                        result=thread_config(sandbox="dangerFullAccess", approval_policy="never"))
    await driver.set_permission_mode("default")
    assert driver.mode == "full-auto"
    await harness.run_turn("recovered")
    assert harness.params(schema.METHOD_TURN_START)["approvalPolicy"] == "never"


@pytest.mark.asyncio
async def test_recovery_restores_full_auto_policy_and_blocks_mismatch(harness):
    driver = await harness.boot(mode="full-auto")
    harness.server.rule(schema.METHOD_THREAD_RESUME, result=thread_config())
    driver._on_notice(RunnerNotice(
        kind=RunnerNoticeKind.RESTARTED, account=ACCOUNT, generation=7,
        recover_via=RECOVER_VIA_THREAD_READ,
    ))
    await harness.settle()
    params = harness.params(schema.METHOD_THREAD_RESUME)
    assert params["sandbox"] == "danger-full-access"
    assert params["approvalPolicy"] == "never"
    assert harness.failures == ["process_crash"]
    with pytest.raises(CodexRunnerUnavailable, match="permission policy"):
        await driver.send_prompt("must not run")
    assert harness.frames(schema.METHOD_TURN_START) == []


@pytest.mark.asyncio
async def test_failed_pending_policy_preserves_queued_work(harness):
    driver = await harness.boot(mode="full-auto")
    await harness.run_turn("first")
    await driver.set_permission_mode("plan")
    harness.server.rule(schema.METHOD_TURN_STEER,
                        error={"code": -32600, "message": "activeTurnNotSteerable"})
    await driver.send_prompt("follow-up")
    harness.server.rule(schema.METHOD_THREAD_RESUME,
                        result=thread_config(sandbox="dangerFullAccess", approval_policy="never"))
    harness.notify_turn_completed()
    await harness.settle()
    assert driver.queued() == ["follow-up"]
    assert len(harness.frames(schema.METHOD_TURN_START)) == 1
    assert driver.counters.pump_errors == 0
    assert driver.current_status() == "needs_attention"

    harness.server.rule(schema.METHOD_THREAD_RESUME,
                        result=thread_config(sandbox="readOnly"))
    await driver.set_permission_mode("plan")
    await harness.settle()
    assert driver.mode == "plan"
    assert driver.queued() == []
    assert driver.turn_active
    assert len(harness.frames(schema.METHOD_TURN_START)) == 2
    assert harness.params(schema.METHOD_TURN_START, index=1)["input"] == [
        {"type": "text", "text": "follow-up"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,sandbox,approval", [
    ("full-auto", "danger-full-access", "never"),
    ("plan", "read-only", "untrusted"),
    ("interactive", "workspace-write", "untrusted"),
])
async def test_resume_and_runner_recovery_keep_selected_policy(harness, mode, sandbox, approval):
    driver = await harness.boot(mode=mode, resume=THREAD)
    assert harness.params(schema.METHOD_THREAD_RESUME)["sandbox"] == sandbox
    driver._on_notice(RunnerNotice(
        kind=RunnerNoticeKind.RESTARTED, account=ACCOUNT, generation=7,
        recover_via=RECOVER_VIA_THREAD_READ,
    ))
    await harness.settle()
    params = harness.params(schema.METHOD_THREAD_RESUME)
    assert params["sandbox"] == sandbox
    assert params["approvalPolicy"] == approval
    assert harness.failures == []


@pytest.mark.asyncio
async def test_assistant_recovery_requests_fresh_tool_surface_without_resuming(harness):
    from server.assistant.options_codex import CodexAssistantOverlay

    overlay = CodexAssistantOverlay(
        base_instructions="test", dynamic_tools=(), handlers={}, cwd="/neutral",
    )
    driver = await harness.boot(mode="full-auto", assistant_overlay=overlay)
    assert harness.params(schema.METHOD_THREAD_START)["sandbox"] == "read-only"
    driver._on_notice(RunnerNotice(
        kind=RunnerNoticeKind.RESTARTED, account=ACCOUNT, generation=7,
        recover_via=RECOVER_VIA_THREAD_READ,
    ))
    await harness.settle()
    assert harness.frames(schema.METHOD_THREAD_RESUME) == []
    assert harness.failures == ["process_crash"]
    with pytest.raises(CodexRunnerUnavailable):
        await driver.send_prompt("must not run without tools")


@pytest.mark.asyncio
async def test_leaving_pending_plan_mode_keeps_full_auto_without_late_switch(harness):
    driver = await harness.boot(mode="full-auto")
    await harness.run_turn("first")
    await driver.set_permission_mode("plan")
    await driver.set_permission_mode("default")
    harness.notify_turn_completed()
    await harness.settle()
    await driver.send_prompt("next")
    assert driver.mode == "full-auto"
    assert harness.frames(schema.METHOD_THREAD_RESUME) == []
    assert harness.params(schema.METHOD_TURN_START)["approvalPolicy"] == "never"


@pytest.mark.asyncio
async def test_mode_acknowledgements_follow_native_verification(harness):
    driver = await harness.boot(mode="full-auto")
    assert [getattr(e, "permission_mode", None) for e in harness.of(ev.SessionMeta)] == [
        "full-auto",
    ]
    await harness.run_turn("first")
    await driver.set_permission_mode("plan")
    await harness.settle()
    assert len(harness.of(ev.SessionMeta)) == 1
    harness.notify_turn_completed()
    await harness.settle()
    await driver.send_prompt("plan next")
    await harness.settle()
    assert harness.of(ev.SessionMeta)[-1].permission_mode == "plan"
    harness.notify_turn_completed()
    await harness.settle()
    harness.server.rule(schema.METHOD_THREAD_RESUME,
                        result=thread_config(sandbox="readOnly"))
    await driver.set_permission_mode("default")
    await harness.settle()
    assert [e.permission_mode for e in harness.of(ev.SessionMeta)] == ["full-auto", "plan"]
    harness.server.rule(schema.METHOD_THREAD_RESUME,
                        result=thread_config(sandbox="dangerFullAccess", approval_policy="never"))
    await driver.set_permission_mode("default")
    await harness.settle()
    assert harness.of(ev.SessionMeta)[-1].permission_mode == "full-auto"


@pytest.mark.asyncio
@pytest.mark.parametrize("select_after_turn", [True, False])
async def test_newer_plan_selection_supersedes_pending_full_auto(harness, select_after_turn):
    driver = await harness.boot(mode="interactive")
    await harness.run_turn("first")
    await driver.set_permission_mode("full-auto")
    if select_after_turn:
        harness.notify_turn_completed()
        await harness.settle()
    await driver.set_permission_mode("plan")
    if not select_after_turn:
        harness.notify_turn_completed()
        await harness.settle()
    await driver.send_prompt("plan only")
    await harness.settle()
    assert driver.mode == "plan"
    assert [
        frame["params"]["sandbox"] for frame in harness.frames(schema.METHOD_THREAD_RESUME)
    ] == ["read-only"]
    assert harness.params(schema.METHOD_TURN_START, index=1)["approvalPolicy"] == "untrusted"
    assert [event.permission_mode for event in harness.of(ev.SessionMeta)] == [
        "interactive", "plan",
    ]


@pytest.mark.asyncio
async def test_repeated_plan_exit_requests_restore_full_auto_after_ack(harness):
    driver = await harness.boot(mode="full-auto")
    await driver.set_permission_mode("plan")
    await harness.run_turn("planning")
    await driver.set_permission_mode("default")
    await driver.set_permission_mode("default")
    await harness.settle()
    assert driver.mode == "plan"
    assert [event.permission_mode for event in harness.of(ev.SessionMeta)] == [
        "full-auto", "plan",
    ]
    harness.notify_turn_completed()
    await harness.settle()
    await driver.send_prompt("implement")
    await harness.settle()
    assert driver.mode == "full-auto"
    assert [frame["params"]["sandbox"]
            for frame in harness.frames(schema.METHOD_THREAD_RESUME)] == [
        "read-only", "danger-full-access",
    ]
    assert harness.params(schema.METHOD_TURN_START, index=1)["approvalPolicy"] == "never"
    assert harness.of(ev.SessionMeta)[-1].permission_mode == "full-auto"


@pytest.mark.asyncio
async def test_reentering_plan_during_pending_exit_preserves_saved_full_auto(harness):
    driver = await harness.boot(mode="full-auto")
    await driver.set_permission_mode("plan")
    await harness.run_turn("planning")
    await driver.set_permission_mode("default")
    await driver.set_permission_mode("plan")
    harness.notify_turn_completed()
    await harness.settle()
    assert driver.mode == "plan"
    await driver.set_permission_mode("default")
    await harness.settle()
    assert driver.mode == "full-auto"
    assert [frame["params"]["sandbox"]
            for frame in harness.frames(schema.METHOD_THREAD_RESUME)] == [
        "read-only", "danger-full-access",
    ]
    await driver.send_prompt("implement")
    assert harness.params(schema.METHOD_TURN_START, index=1)["approvalPolicy"] == "never"


@pytest.mark.asyncio
async def test_empty_native_thread_policy_switch_restarts_without_wedging(harness):
    driver = await harness.boot(mode="full-auto")
    harness.server.rule(schema.METHOD_THREAD_RESUME,
                        error={"code": -32600, "message": "no rollout found"})
    harness.server.rule(schema.METHOD_THREAD_START,
                        result=thread_config("empty-plan", sandbox="readOnly"))
    await driver.set_permission_mode("plan")
    assert driver.mode == "plan"
    assert harness.engine_ids[-1] == (SESSION, "empty-plan")
    assert driver.current_status() != "needs_attention"
    await driver.send_prompt("first prompt")
    assert harness.params(schema.METHOD_TURN_START)["threadId"] == "empty-plan"
    assert harness.params(schema.METHOD_TURN_START)["approvalPolicy"] == "untrusted"


@pytest.mark.asyncio
async def test_loaded_native_policy_switch_forks_history_and_updates_resume_handle(harness):
    driver = await harness.boot(mode="full-auto", resume=THREAD)
    harness.server.rule(schema.METHOD_THREAD_RESUME,
                        result=thread_config(sandbox="dangerFullAccess", approval_policy="never"))
    harness.server.rule("thread/backgroundTerminals/list", result={"data": [], "nextCursor": None})
    harness.server.rule("thread/fork", result=thread_config("fork-plan", sandbox="readOnly"))
    await driver.set_permission_mode("plan")
    assert driver.mode == "plan"
    assert harness.engine_ids[-1] == (SESSION, "fork-plan")
    await driver.send_prompt("keep the previous context")
    assert harness.params(schema.METHOD_TURN_START)["threadId"] == "fork-plan"
    assert harness.params("thread/fork")["threadId"] == THREAD


@pytest.mark.asyncio
async def test_loaded_native_policy_switch_does_not_orphan_background_work(harness):
    driver = await harness.boot(mode="full-auto", resume=THREAD)
    harness.server.rule(schema.METHOD_THREAD_RESUME,
                        result=thread_config(sandbox="dangerFullAccess", approval_policy="never"))
    harness.server.rule("thread/backgroundTerminals/list", result={"data": [{
        "itemId": "item-bg", "processId": "123", "command": "npm run dev", "cwd": "/work",
    }], "nextCursor": None})
    await driver.set_permission_mode("plan")
    assert driver.mode == "full-auto"
    assert not harness.frames("thread/fork")
    await driver.send_prompt("stop the background job")
    assert harness.params(schema.METHOD_TURN_START)["threadId"] == THREAD


@pytest.mark.asyncio
async def test_guard_refusal_keeps_execution_blocked_when_original_policy_cannot_be_verified(harness):
    driver = await harness.boot(mode="full-auto", resume=THREAD)

    def mismatched_policy(params):
        if params["sandbox"] == "read-only":
            return thread_config(sandbox="dangerFullAccess", approval_policy="never")
        return thread_config(sandbox="readOnly")

    harness.server.rule(schema.METHOD_THREAD_RESUME, result=mismatched_policy)
    harness.server.rule(schema.METHOD_THREAD_BACKGROUND_TERMINALS_LIST, result={"data": [{
        "itemId": "item-bg", "processId": "123", "command": "npm run dev", "cwd": "/work",
    }], "nextCursor": None})
    await driver.set_permission_mode("plan")
    assert driver.mode == "full-auto"
    assert not harness.frames(schema.METHOD_THREAD_FORK)
    assert driver.current_status() == "needs_attention"
    with pytest.raises(CodexRunnerUnavailable):
        await driver.send_prompt("cannot trust the retained mode label")
    assert not harness.frames(schema.METHOD_TURN_START)


@pytest.mark.asyncio
async def test_policy_thread_persistence_failure_blocks_until_retry(harness):
    driver = await harness.boot(mode="full-auto")
    harness.server.rule(schema.METHOD_THREAD_RESUME,
                        error={"code": -32600, "message": "no rollout found"})
    harness.server.rule(schema.METHOD_THREAD_START,
                        result=thread_config("empty-plan", sandbox="readOnly"))
    original = driver._on_engine_session_id

    async def fail_save(session_id, thread_id):
        raise RuntimeError("database unavailable")

    driver._on_engine_session_id = fail_save
    await driver.set_permission_mode("plan")
    assert driver.mode == "full-auto"
    with pytest.raises(CodexRunnerUnavailable):
        await driver.send_prompt("must not run before saved")
    driver._on_engine_session_id = original
    await driver.set_permission_mode("plan")
    assert driver.mode == "plan"
    assert harness.engine_ids[-1] == (SESSION, "empty-plan")
    await driver.send_prompt("saved")
    assert harness.params(schema.METHOD_TURN_START)["threadId"] == "empty-plan"


@pytest.mark.asyncio
async def test_failed_native_turn_start_cannot_be_replaced_as_empty(harness):
    driver = await harness.boot(mode="full-auto")
    harness.server.rule(schema.METHOD_TURN_START,
                        error={"code": -32000, "message": "ambiguous turn failure"})
    await driver.send_prompt("may have persisted")
    harness.server.rule(schema.METHOD_THREAD_RESUME,
                        error={"code": -32600, "message": "no rollout found"})
    await driver.set_permission_mode("plan")
    assert driver.mode == "full-auto"
    assert len(harness.frames(schema.METHOD_THREAD_START)) == 1
    with pytest.raises(CodexRunnerUnavailable):
        await driver.send_prompt("must not drop history")


@pytest.mark.asyncio
async def test_fork_persistence_retry_retries_save_before_allowing_new_work(harness):
    driver = await harness.boot(mode="full-auto", resume=THREAD)
    harness.server.rule(schema.METHOD_THREAD_RESUME,
                        result=thread_config(sandbox="dangerFullAccess", approval_policy="never"))
    harness.server.rule(schema.METHOD_THREAD_FORK,
                        result=thread_config("fork-plan", sandbox="readOnly"))
    original = driver._on_engine_session_id
    attempts = []

    async def fail_save(session_id, thread_id):
        attempts.append(thread_id)
        # Saving yields to other session requests. A matching native resume on
        # retry must not make prompts runnable during that durability gap.
        await asyncio.sleep(0)
        assert driver.current_status() == "needs_attention"
        with pytest.raises(CodexRunnerUnavailable):
            await driver.send_prompt("save is still in progress")
        raise RuntimeError("database unavailable")

    driver._on_engine_session_id = fail_save
    await driver.set_permission_mode("plan")
    harness.server.rule(schema.METHOD_THREAD_RESUME,
                        result=thread_config("fork-plan", sandbox="readOnly"))
    await driver.set_permission_mode("plan")
    assert attempts == ["fork-plan", "fork-plan"]
    assert driver.mode == "full-auto"
    with pytest.raises(CodexRunnerUnavailable):
        await driver.send_prompt("must not use an unpersisted fork")
    driver._on_engine_session_id = original
    await driver.set_permission_mode("plan")
    assert harness.engine_ids[-1] == (SESSION, "fork-plan")
    assert driver.mode == "plan"
    await driver.send_prompt("saved fork")
    assert harness.params(schema.METHOD_TURN_START)["threadId"] == "fork-plan"


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [None, THREAD])
async def test_native_goal_prevents_policy_fork_without_losing_goal_accounting(harness, resume):
    driver = await harness.boot(mode="full-auto", resume=resume)
    harness.server.rule(schema.METHOD_THREAD_RESUME,
                        result=thread_config(sandbox="dangerFullAccess", approval_policy="never"))
    if resume is None:
        harness.server.rule(schema.METHOD_THREAD_RESUME,
                            error={"code": -32600, "message": "no rollout found"})
    harness.server.rule("thread/goal/get", result={"goal": {
        "threadId": THREAD, "objective": "Keep working", "status": "active",
        "tokenBudget": 10000, "tokensUsed": 42, "timeUsedSeconds": 3,
        "createdAt": 1, "updatedAt": 2,
    }})
    await driver.set_permission_mode("plan")
    await harness.settle()
    assert not harness.frames(schema.METHOD_THREAD_FORK)
    assert harness.frames("thread/goal/get")
    assert driver.mode == "full-auto"
    assert "goal" in harness.of(ev.ErrorEvent)[-1].message
    assert len(harness.frames(schema.METHOD_THREAD_START)) == (1 if resume is None else 0)
    await driver.send_prompt("continue the original goal")
    assert harness.params(schema.METHOD_TURN_START)["threadId"] == THREAD
