"""Native boundary smoke; opt in with VIBERPIT_TEST_CODEX_FULL_AUTO=1.

Uses the pinned binary, private empty runtime home and canonical production
profile. App-server start needs no authentication. The workflow and resume test additionally
requires VIBERPIT_TEST_CODEX_FULL_AUTO_ACCOUNT and authority client settings; it
spends real model quota and counts approval callbacks. GitHub PR creation remains
outside this disposable local-origin harness. Explicit forbidden rules are checked
with the native execpolicy evaluator, separately from model-driven execution.
No account credentials are read/copied and no remote repository is changed.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from server.codex import schema
from server.codex.permissions import profile_for_mode
from server.codex.process import CodexProcess, build_child_env, ensure_codex_home
from server.codex.protocol import CodexAppServerClient

pytestmark = pytest.mark.skipif(
    os.environ.get("VIBERPIT_TEST_CODEX_FULL_AUTO") != "1",
    reason="set VIBERPIT_TEST_CODEX_FULL_AUTO=1 for the real Codex boundary smoke",
)


@pytest.fixture
def native(tmp_path: Path) -> tuple[str, Path]:
    binary = shutil.which("codex")
    assert binary, "the pinned Codex binary must be installed"
    home = ensure_codex_home(tmp_path / "runtime", "acceptance")
    version = subprocess.run(
        [binary, "--version"], check=True, capture_output=True, text=True,
        env=build_child_env(home), timeout=15,
    )
    assert version.stdout.strip() == schema.PINNED_CLI_VERSION
    return binary, home


def test_native_execpolicy_keeps_explicit_forbidden_rules(
    native: tuple[str, Path],
) -> None:
    """Deterministic native rule evaluation, not a model execution assertion.

    A model may refuse a forbidden command before tool execution. The pinned
    event API then emits no CommandExecutionItem, so model prose cannot prove
    runtime enforcement. Check the actual native rule evaluator independently
    instead of making live acceptance depend on the model choosing to violate it.
    """
    binary, home = native
    profile = profile_for_mode("full-auto")
    rules = Path(__file__).resolve().parents[2] / ".codex" / "rules" / "default.rules"
    for flag in ("--force", "-f", "--force-with-lease"):
        checked = subprocess.run(
            [binary, "-c", f'sandbox_mode="{profile.sandbox_mode.value}"',
             "-c", f'approval_policy="{profile.approval_policy.value}"',
             "execpolicy", "check", "--rules", str(rules),
             "git", "push", flag, "origin", "acceptance"],
            env=build_child_env(home), capture_output=True, text=True, check=True, timeout=15,
        )
        result = json.loads(checked.stdout)
        if flag == "--force-with-lease":
            assert result.get("decision") != "forbidden"
            assert result["matchedRules"] == []
        else:
            assert result["decision"] == "forbidden"
            assert result["matchedRules"]


@pytest.mark.asyncio
async def test_native_start_reports_full_auto_policy(
    native: tuple[str, Path], tmp_path: Path,
) -> None:
    binary, home = native
    profile = profile_for_mode("full-auto")
    process = CodexProcess(binary, home)
    await process.start()
    client = CodexAppServerClient(process.transport, external_auth_enabled=False)
    try:
        await client.start()
        await client.initialize()
        started = await client.start_thread(schema.ThreadStartParams(
            cwd=str(tmp_path), sandbox=profile.sandbox_mode.value,
            approval_policy=profile.approval_policy.value,
        ))
        assert started.sandbox == profile.sandbox_mode.policy_type
        assert started.approval_policy == profile.approval_policy.value
        assert client.counters.unhandled_server_requests == 0
        assert client.counters.unknown_server_requests == 0
    finally:
        await client.aclose()
        await process.stop()
    assert not (home / "auth.json").exists()


@pytest.mark.asyncio
async def test_native_full_auto_executes_worktree_network_and_socket_workflow(
    native: tuple[str, Path], tmp_path: Path,
) -> None:
    account = os.environ.get("VIBERPIT_TEST_CODEX_FULL_AUTO_ACCOUNT")
    if not account:
        pytest.skip("set VIBERPIT_TEST_CODEX_FULL_AUTO_ACCOUNT to authorize a paid live turn")
    from dotenv import dotenv_values

    from server.codex.auth_client import AuthorityClient
    from server.codex.external_auth import ExternalAuthProvider

    # Optional operator-supplied service files; only authority configuration enters
    # this process. Tokens still travel through the production mTLS provider only.
    for filename in os.environ.get("VIBERPIT_TEST_CODEX_AUTH_ENV_FILES", "").split(":"):
        if filename:
            for key, value in dotenv_values(filename).items():
                if key.startswith("VIBERPIT_CODEX_AUTHORITY_") and value is not None:
                    os.environ[key] = value
    provider = ExternalAuthProvider(AuthorityClient.from_env())
    binary, home = native
    profile = profile_for_mode("full-auto")
    # Put Git metadata and origin outside the native thread's actual cwd.
    # /dev/shm is useful for running this smoke: Codex grants /tmp separately.
    main, work, remote = tmp_path / "main", tmp_path / "linked", tmp_path / "remote.git"
    for cwd, args in (
        (tmp_path, ["init", "--bare", "-q", str(remote)]),
        (tmp_path, ["init", "-q", "-b", "main", str(main)]),
        (main, ["config", "user.name", "Acceptance Harness"]),
        (main, ["config", "user.email", "acceptance@example.invalid"]),
        (main, ["commit", "--allow-empty", "-qm", "initial"]),
        (main, ["remote", "add", "origin", str(remote)]),
        (main, ["worktree", "add", "-q", "-b", "acceptance", str(work)]),
    ):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, timeout=15)
    gitdir = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"], cwd=work,
        check=True, capture_output=True, text=True, timeout=10,
    )
    assert not Path(gitdir.stdout.strip()).is_relative_to(work)
    probe = work / "probe.py"
    probe.write_text('''
import http.server
import pathlib
import subprocess
import sys
import threading
import urllib.request

work = pathlib.Path.cwd()
phase = sys.argv[1]
assert phase in ("started", "resumed", "transitioned")
remote = work.parent / "remote.git"
def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()
(work / "result.txt").write_text(f"full-auto {phase} accepted\\n")
git(work, "add", "result.txt")
git(work, "commit", "-qm", f"acceptance {phase}")
git(work, "push", "-q", "origin", "acceptance")
assert git(remote, "show", "acceptance:result.txt") == f"full-auto {phase} accepted"
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"socket accepted")
    def log_message(self, *args):
        pass
server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}", timeout=10) as r:
        assert r.read() == b"socket accepted"
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
with urllib.request.urlopen("https://example.com", timeout=20) as r:
    assert r.status == 200
(work / f"PROBE_DONE_{phase}").write_text("passed")
print("edit commit linked-worktree push local-http outbound-https: passed")
''')
    # Run positive workflow with the actual repository rules loaded, too.
    rules = Path(__file__).resolve().parents[2] / ".codex" / "rules" / "default.rules"
    (home / "rules").mkdir()
    shutil.copyfile(rules, home / "rules" / rules.name)
    approvals: list[str] = []
    tool_calls: list[tuple[str, str]] = []

    async def record_tool(params: schema.DynamicToolCallParams) -> schema.DynamicToolCallResponse:
        assert params.tool == "policy_probe_echo"
        tool_calls.append((params.thread_id, params.arguments["phase"]))
        return schema.DynamicToolCallResponse(
            success=True, content_items=(schema.DynamicToolCallContentItem("passed"),),
        )

    async def reject_approval(
        method: str, params: schema.ApprovalParams,
    ) -> schema.ApprovalResponse:
        approvals.append(method)
        if isinstance(params, schema.FileChangeApprovalParams):
            return schema.FileChangeApprovalResponse(decision="decline")
        return schema.CommandExecutionApprovalResponse(decision="decline")

    process = CodexProcess(binary, home)
    await process.start()
    client = CodexAppServerClient(
        process.transport, external_auth_enabled=True,
        refresh_handler=provider.refresh_handler_for(account),
        approval_handler=reject_approval,
        dynamic_tool_handler=record_tool,
    )
    try:
        await client.start()
        await client.initialize()
        await client.login_with_external_tokens(await provider.login_params(account))
        started = await client.start_thread(schema.ThreadStartParams(
            cwd=str(work), sandbox=profile.sandbox_mode.value,
            approval_policy=profile.approval_policy.value,
            dynamic_tools=(schema.FunctionDynamicToolSpec(
                name="policy_probe_echo", description="Record acceptance phase; returns passed.",
                input_schema={"type": "object", "properties": {"phase": {"type": "string"}},
                              "required": ["phase"], "additionalProperties": False},
            ),),
        ))
        assert started.sandbox == profile.sandbox_mode.policy_type
        assert started.approval_policy == profile.approval_policy.value
        thread_id = started.thread.id
        for phase in ("started", "resumed", "transitioned"):
            if phase == "resumed":
                resumed = await client.resume_thread(schema.ThreadResumeParams(
                    thread_id=thread_id, cwd=str(work),
                    sandbox=profile.sandbox_mode.value,
                    approval_policy=profile.approval_policy.value,
                ))
                assert resumed.sandbox == profile.sandbox_mode.policy_type
                assert resumed.approval_policy == profile.approval_policy.value
            if phase == "transitioned":
                for requested in (profile_for_mode("plan"), profile):
                    forked = await client.fork_thread(schema.ThreadForkParams(
                        thread_id=thread_id, cwd=str(work),
                        sandbox=requested.sandbox_mode.value,
                        approval_policy=requested.approval_policy.value,
                    ))
                    assert forked.sandbox == requested.sandbox_mode.policy_type
                    assert forked.approval_policy == requested.approval_policy.value
                    thread_id = forked.thread.id
            turn = await client.start_turn(schema.TurnStartParams(
                thread_id=thread_id,
                input=(schema.TextUserInput(
                    f"First call policy_probe_echo with phase {phase!r}. "
                    f"Run {sys.executable} {probe} {phase} exactly once in {work}. "
                    "This authorized acceptance script edits and commits in this linked "
                    "worktree, pushes to a disposable local bare origin, opens a loopback "
                    "test server and checks https://example.com. "
                    f"Then create MODEL_DONE_{phase} containing passed in the cwd. "
                    "Do not ask for confirmation."
                ),),
            ))
            async with asyncio.timeout(240):
                while True:
                    _, params = await client.next_notification()
                    if (isinstance(params, schema.TurnCompletedParams)
                            and params.turn.id == turn.turn.id):
                        assert params.turn.status.value == "completed"
                        break
            assert (thread_id, phase) in tool_calls, "native tool declaration was lost"
            assert not approvals, "the model requested routine tool approval"
            assert (work / f"PROBE_DONE_{phase}").read_text() == "passed"
            assert (work / f"MODEL_DONE_{phase}").read_text().strip() == "passed"
            pushed = subprocess.run(
                ["git", "--git-dir", str(remote), "show", "acceptance:result.txt"],
                check=True, capture_output=True, text=True, timeout=10,
            )
            assert pushed.stdout.strip() == f"full-auto {phase} accepted"
        assert client.counters.unhandled_server_requests == 0
    finally:
        await client.aclose()
        await process.stop()
    assert not (home / "auth.json").exists()


@pytest.mark.asyncio
async def test_native_policy_roundtrip_preserves_persisted_history(
    native: tuple[str, Path], tmp_path: Path,
) -> None:
    """No auth/quota: interrupt a native turn, then verify both fork policies.

    The native thread/resume response for a loaded thread describes its existing
    settings, ignoring policy overrides. Fork is the acknowledged transition;
    these assertions cover the exact boundary that an echoing fake concealed.
    """
    from server.codex.protocol import CodexRequestFailed

    binary, home = native
    process = CodexProcess(binary, home)
    await process.start()
    client = CodexAppServerClient(process.transport, external_auth_enabled=False)
    full = profile_for_mode("full-auto")
    plan = profile_for_mode("plan")
    try:
        await client.start()
        await client.initialize()
        started = await client.start_thread(schema.ThreadStartParams(
            cwd=str(tmp_path), sandbox=full.sandbox_mode.value,
            approval_policy=full.approval_policy.value,
        ))
        with pytest.raises(CodexRequestFailed) as failure:
            await client.resume_thread(schema.ThreadResumeParams(
                thread_id=started.thread.id, cwd=str(tmp_path),
                sandbox=plan.sandbox_mode.value, approval_policy=plan.approval_policy.value,
            ))
        assert failure.value.code == -32600
        # No input has been sent, so replacement is lossless and acknowledged.
        empty_plan = await client.start_thread(schema.ThreadStartParams(
            cwd=str(tmp_path), sandbox=plan.sandbox_mode.value,
            approval_policy=plan.approval_policy.value,
        ))
        assert empty_plan.sandbox == plan.sandbox_mode.policy_type
        assert empty_plan.approval_policy == plan.approval_policy.value
        turn = await client.start_turn(schema.TurnStartParams(
            thread_id=started.thread.id,
            input=(schema.TextUserInput("Preserve this transition acceptance message."),),
        ))
        async with asyncio.timeout(30):
            while True:
                _, notice = await client.next_notification()
                if (isinstance(notice, schema.ItemCompletedParams)
                        and notice.turn_id == turn.turn.id
                        and isinstance(notice.item, schema.UserMessageItem)):
                    break
            await client.interrupt_turn(schema.TurnInterruptParams(
                thread_id=started.thread.id, turn_id=turn.turn.id,
            ))
            while True:
                _, notice = await client.next_notification()
                if isinstance(notice, schema.TurnCompletedParams) and notice.turn.id == turn.turn.id:
                    assert notice.turn.status.value in {"interrupted", "failed"}
                    break
        retained = await client.resume_thread(schema.ThreadResumeParams(
            thread_id=started.thread.id, cwd=str(tmp_path),
            sandbox=plan.sandbox_mode.value, approval_policy=plan.approval_policy.value,
        ))
        assert retained.sandbox == full.sandbox_mode.policy_type
        assert retained.approval_policy == full.approval_policy.value
        previous = started.thread.id
        goal = await client.get_thread_goal(schema.ThreadGoalGetParams(thread_id=previous))
        assert not goal.has_goal
        for profile in (plan, full):
            forked = await client.fork_thread(schema.ThreadForkParams(
                thread_id=previous, cwd=str(tmp_path),
                sandbox=profile.sandbox_mode.value,
                approval_policy=profile.approval_policy.value,
            ))
            assert forked.thread.id != previous
            assert forked.sandbox == profile.sandbox_mode.policy_type
            assert forked.approval_policy == profile.approval_policy.value
            assert len(forked.thread.turns) == 1
            assert forked.thread.turns[0].id == turn.turn.id
            assert forked.thread.turns[0].items
            previous = forked.thread.id
        assert client.counters.unhandled_server_requests == 0
    finally:
        await client.aclose()
        await process.stop()
    assert not (home / "auth.json").exists()


@pytest.mark.asyncio
async def test_native_driver_empty_thread_policy_roundtrip(
    native: tuple[str, Path], tmp_path: Path,
) -> None:
    """Exercise the actual driver against native RPCs before any turn exists.

    Only process ownership/routing is stubbed; start/resume/goal/background
    responses and permission acknowledgements come from the pinned binary.
    """
    from unittest.mock import Mock

    from server.codex.driver import CodexAgentDriver
    from server.codex.manager import CodexRunnerManager
    from server.sessions.permissions import PermissionRegistry

    binary, home = native
    process = CodexProcess(binary, home)
    await process.start()
    client = CodexAppServerClient(process.transport, external_auth_enabled=False)
    manager = Mock(spec=CodexRunnerManager)
    manager.client_for.return_value = client
    manager.generation_for.return_value = 1
    handles: list[str] = []

    async def save_handle(session_id: str, thread_id: str) -> None:
        handles.append(thread_id)

    driver = CodexAgentDriver(
        "native-empty", "acceptance", str(tmp_path), str(home), mode="full-auto",
        manager=manager, registry=PermissionRegistry(), identity="acceptance",
        on_engine_session_id=save_handle,
    )
    try:
        await client.start()
        await client.initialize()
        await driver.start()
        assert driver.mode == "full-auto"
        allocated = [driver._require_thread()]
        await driver.set_permission_mode("plan")
        assert driver.mode == "plan"
        assert driver.current_status() != "needs_attention"
        allocated.append(driver._require_thread())
        await driver.set_permission_mode("default")
        assert driver.mode == "full-auto"
        assert driver.current_status() != "needs_attention"
        allocated.append(driver._require_thread())
        assert len(set(allocated)) == 3
        # Empty native allocations cannot resume after a runner restart. Policy
        # replacement must preserve #1499's first-turn publication boundary.
        assert not handles
    finally:
        await driver.aclose()
        await client.aclose()
        await process.stop()
    assert not (home / "auth.json").exists()
