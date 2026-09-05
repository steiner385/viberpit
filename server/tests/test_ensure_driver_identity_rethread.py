"""Regression: ensure_driver must re-thread a real identity onto a driver that was
first built under the internal "system" identity.

The bug: after a daemon restart, sessions are rehydrated and their drivers are
eagerly re-launched by background/dispatch paths with identity="system"
(ensure_driver's default). When the user later attaches over the WebSocket,
ensure_driver's fast path returned the existing "system" driver WITHOUT updating
its identity. AskUserQuestion / permission answers are registered under
driver.identity, and QuestionRegistry.resolve() rejects answers whose identity
doesn't match — so every answer from the real user was silently dropped (RPC still
200) and the agent hung on the question forever ("lost ability to interact").

ensure_driver must upgrade a "system" driver's identity to the attaching user's
real identity, and must never downgrade a real identity back to "system".
"""

import asyncio
from unittest.mock import patch

from server.db import init_db, db_session
from server.db.models import Session as DbSession
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.permissions import PermissionRegistry
from server.sessions.types import SYSTEM_IDENTITY
from server.tests.fakes.fake_factory import fake_sdk_factory


async def _seed(rows):
    async with db_session() as db:
        for r in rows:
            db.add(DbSession(**r))


async def _ensure(orch, tmp_path, sid, identity=None):
    """Call ensure_driver with compose/configdir patched so no real creds are needed.

    The await MUST happen inside the patch context, otherwise the real compose()
    (which reads account credentials off disk) runs when the coroutine is driven.
    """
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    with patch("server.sessions.orchestrator.compose", return_value=cfg_dir), \
         patch("server.sessions.orchestrator.configdir"):
        if identity is None:
            return await orch.ensure_driver(sid)
        return await orch.ensure_driver(sid, identity)


def test_ensure_driver_upgrades_system_identity_to_real_user(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([dict(id="s1", name="S1", account_name="acct1",
                          engine_session_id="cs1", status="idle", cwd=str(tmp_path))])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        # 1) Internal/background path builds the driver first, under "system".
        d_sys = await _ensure(orch, tmp_path, "s1")  # default identity="system"
        first_identity = d_sys.identity
        # 2) The real user attaches over WS — same driver instance returned (fast path)…
        d_user = await _ensure(orch, tmp_path, "s1", "user@example.test")
        # …and its identity must now be the real user's, so answers resolve.
        return d_sys is d_user, first_identity, d_user.identity

    same_driver, first_identity, upgraded_identity = asyncio.run(run())
    assert same_driver, "expected the existing driver to be reused (fast path)"
    assert first_identity == SYSTEM_IDENTITY
    assert upgraded_identity == "user@example.test", (
        "ensure_driver must re-thread the attaching user's identity onto a "
        "system-built driver so AskUserQuestion answers are accepted"
    )


def test_ensure_driver_does_not_downgrade_real_identity_to_system(tmp_path):
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([dict(id="s2", name="S2", account_name="acct1",
                          engine_session_id="cs2", status="idle", cwd=str(tmp_path))])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        await _ensure(orch, tmp_path, "s2", "user@example.test")  # real user attaches first
        d = await _ensure(orch, tmp_path, "s2")  # later internal call defaults to "system"
        return d.identity

    identity = asyncio.run(run())
    assert identity == "user@example.test", (
        "an internal 'system' call must not clobber a real user's identity"
    )


def test_rethread_makes_question_asked_under_system_answerable(tmp_path):
    """Regression (2026-09-04, session dee29111 on dobby): after a deploy restart the
    daemon rebuilt the driver under "system" and the CLI re-asked its AskUserQuestion
    in that turn BEFORE anyone attached. ensure_driver then upgraded driver.identity
    on attach, but the question was already registered under "system", so
    QuestionRegistry.resolve() rejected the user's answer as an identity mismatch
    ("That question is no longer pending") even though pending_questions() listed it.

    Re-threading must carry the pending asks along with the driver's identity."""
    db_url = f"sqlite+aiosqlite:///{tmp_path/'ccswitch.db'}"

    async def run():
        await init_db(db_url=db_url)
        await _seed([dict(id="s3", name="S3", account_name="acct1",
                          engine_session_id="cs3", status="idle", cwd=str(tmp_path))])
        orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
        await orch.rehydrate()
        d_sys = await _ensure(orch, tmp_path, "s3")  # built by an internal path
        # The CLI asks a question while the driver still carries "system" — exactly
        # what ClaudeAgentDriver's AskUserQuestion handler registers.
        qid, fut = d_sys._qreg.request("s3", "t1", "H", "Q?", [{"label": "A"}], False,
                                       d_sys.identity)
        pid, pfut = await d_sys._reg.request(
            "s3", "t1", "Bash", {"command": "make"}, mode="interactive",
            denylist=PermissionRegistry.DANGEROUS, allowlist=True, identity=d_sys.identity)
        # Now the real user attaches (WS attach → ensure_driver with their identity).
        await _ensure(orch, tmp_path, "s3", "user@example.test")
        listed = [q["question_id"] for q in orch.pending_questions("s3")]
        answered = await orch.answer_question("s3", qid, ["A"], "user@example.test")
        approved = orch.resolve_permission("s3", pid, "allow", "user@example.test")
        return qid, listed, answered, approved, await fut, await pfut

    qid, listed, answered, approved, result, perm = asyncio.run(run())
    assert listed == [qid], "the question is still pending — only the identity gate blocked it"
    assert answered is True, "a question the CLI asked under 'system' must accept the attached user's answer"
    assert approved is True, "same for a permission prompt registered under 'system'"
    assert result == (["A"], "option")
    assert perm is True
