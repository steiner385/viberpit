"""A daemon restart must not brand its own SIGTERM'd turns "Turn failed".

Root cause (2026-08-29, rocky journal): `KillMode=control-group` SIGTERMs every live
claude subprocess in parallel with the daemon itself. Each child dies with exit code
143; the driver drain classifies "Command failed with exit code 143" as a
non-recoverable failure (the crash regex only matched NEGATIVE exit codes) and
persists ``needs_attention`` before the old daemon exits. ``rehydrate()`` only
normalizes mid-turn statuses, so the new daemon shows a spurious "Turn failed" card
instead of "interrupted by restart — re-send to resume" (issue #434).

Three layers pin the fix:

1. ``classify`` treats signal exits (code >= 128: 143=SIGTERM, 137=SIGKILL/OOM) as
   ``process_crash`` — recoverable, so a live daemon relaunches the driver in place
   and a shutting-down daemon skips escalation entirely.
2. ``_persist_status`` refuses to write ``needs_attention`` while ``_shutting_down``
   — the row keeps its mid-turn status for the next boot's rehydrate() to normalize.
3. The daemon systemd units use ``KillMode=mixed`` so SIGTERM reaches ONLY the
   daemon process: children stay alive until ``orchestrator.stop()`` (which sets
   ``_shutting_down`` first) tears them down in order. control-group delivery is the
   race that defeats layer 2's flag.
"""
import asyncio
from pathlib import Path

from server.sessions.failure import classify
from server.sessions.orchestrator import SessionOrchestrator
from server.sessions.types import SessionRecord
from server.tests.fakes.fake_factory import fake_sdk_factory

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Layer 1 — classification
# ---------------------------------------------------------------------------

def test_sigterm_exit_143_classifies_as_process_crash():
    """The exact drain surface from the incident journal."""
    f = classify("driver_error",
                 "Exception: Command failed with exit code 143 (exit code: 143)")
    assert f.kind == "process_crash"
    assert f.is_recoverable


def test_sigkill_exit_137_classifies_as_process_crash():
    """OOM-killed / hard-killed children are the same shape one signal over."""
    f = classify("driver_error", "Command failed with exit code 137")
    assert f.kind == "process_crash"


def test_plain_nonzero_exit_stays_other():
    """A genuine CLI failure (exit 1) is NOT a signal death — keep surfacing it."""
    f = classify("driver_error", "Command failed with exit code 1")
    assert f.kind == "other"
    assert not f.is_recoverable


def test_negative_signal_exit_still_classifies_as_process_crash():
    """The pre-existing negative-code spelling must keep working."""
    f = classify("driver_error", "process exited with exit code: -6")
    assert f.kind == "process_crash"


# ---------------------------------------------------------------------------
# Layer 2 — persist gate during shutdown
# ---------------------------------------------------------------------------

def _record(sid: str, status: str) -> SessionRecord:
    return SessionRecord(
        id=sid, name=sid, account="acct", claude_session_id="c",
        cols=80, rows=24, cwd="/tmp", status=status,
    )


def _orch_with_db(tmp_path, name: str) -> tuple[SessionOrchestrator, str]:
    db_url = f"sqlite+aiosqlite:///{tmp_path / name}.db"
    orch = SessionOrchestrator(sdk_factory=fake_sdk_factory())
    return orch, db_url


def _seed_row(db_url: str, sid: str, status: str, cwd: str) -> None:
    from server.db import db_session, init_db
    from server.db.models import Session as DbSession

    async def seed():
        await init_db(db_url=db_url)
        async with db_session() as db:
            db.add(DbSession(id=sid, name=sid, account_name="acct",
                             claude_session_id="c", status=status, cwd=cwd))
    asyncio.run(seed())


def _row_status(sid: str) -> str:
    from sqlalchemy import select as sa_select

    from server.db import db_session
    from server.db.models import Session as DbSession

    async def read():
        async with db_session() as db:
            return (await db.execute(
                sa_select(DbSession.status).where(DbSession.id == sid)
            )).scalar_one()
    return asyncio.run(read())


def test_needs_attention_not_persisted_during_shutdown(tmp_path):
    """A turn failure surfacing mid-teardown must leave the mid-turn row for
    rehydrate() to normalize to idle + interruptedByRestart."""
    orch, db_url = _orch_with_db(tmp_path, "shutdown-gate")
    _seed_row(db_url, "s1", "working", str(tmp_path))
    orch._records["s1"] = _record("s1", "working")
    orch._shutting_down = True

    asyncio.run(orch._persist_status("s1", "needs_attention"))

    assert orch._records["s1"].status == "working"
    assert _row_status("s1") == "working"


def test_needs_attention_still_persists_when_not_shutting_down(tmp_path):
    """The gate is shutdown-scoped: a real mid-run failure still surfaces."""
    orch, db_url = _orch_with_db(tmp_path, "no-gate")
    _seed_row(db_url, "s2", "working", str(tmp_path))
    orch._records["s2"] = _record("s2", "working")

    asyncio.run(orch._persist_status("s2", "needs_attention"))

    assert orch._records["s2"].status == "needs_attention"
    assert _row_status("s2") == "needs_attention"


def test_ended_still_persists_during_shutdown(tmp_path):
    """The gate must not block terminal writes teardown legitimately performs."""
    orch, db_url = _orch_with_db(tmp_path, "ended-ok")
    _seed_row(db_url, "s3", "working", str(tmp_path))
    orch._records["s3"] = _record("s3", "working")
    orch._shutting_down = True

    asyncio.run(orch._persist_status("s3", "ended"))

    assert orch._records["s3"].status == "ended"
    assert _row_status("s3") == "ended"


# ---------------------------------------------------------------------------
# Layer 3 — unit files must not SIGTERM the children alongside the daemon
# ---------------------------------------------------------------------------

def test_daemon_units_use_killmode_mixed():
    """control-group delivery kills claude children BEFORE stop() can set
    _shutting_down — the exact race behind the 2026-08-29 "Turn failed" batch.
    mixed sends SIGTERM only to the daemon; children are torn down in order by
    orchestrator.stop() (final SIGKILL still sweeps the cgroup at TimeoutStopSec)."""
    for unit in ("ccswitch-daemon.service", "ccswitch-daemon-remote.service"):
        text = (REPO_ROOT / "deploy" / unit).read_text()
        assert "KillMode=mixed" in text, f"{unit} must use KillMode=mixed"
        assert "KillMode=control-group" not in text, (
            f"{unit} must not SIGTERM the whole cgroup — that races stop()'s "
            f"_shutting_down flag and brands interrupted turns needs_attention")
