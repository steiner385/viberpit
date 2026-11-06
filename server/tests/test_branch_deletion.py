"""Deletion gated on ownership (D-22, Task 2): a branch is ``git branch -D``'d only when
ViberPit recorded creating it (or it is the strict legacy shape of the session being
removed), the record goes with it, and the Discard dialog's "will be deleted / kept"
is the deletion's own answer.

Plan: docs/superpowers/plans/2026-11-06-desktop-parity-d22-branch-prefix.md, Task 2.
Review Focus 1 (no path deletes an unrecorded branch; an unknown read keeps it),
3 (preflight ``sessionBranch`` == the outcome) and 5 (a rocky record never authorises a
delete on dobby). Real git under ``tmp_path``, with a local bare ``origin``.
"""
from __future__ import annotations

import ast
import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy import select, update
from unittest.mock import patch

from viberpit import worktrees
from server.db import db_session
from server.db.models import Session as DbSession, SessionBranch
from server.sessions import branch_ownership as bo
from server.sessions.orchestrator import SessionOrchestrator
# Fixtures and helpers reused from the controls, fork and move suites.
from server.tests.test_worktree_controls import (  # noqa: F401
    SID, _branch_exists, _git, _orch, env,
)
from server.tests.test_session_move import (  # noqa: F401
    SID as MOVE_SID, _managed, _manifest, _source, _target, two_hosts, visibility,
)

OTHER = "eeee9999-5678-0000-0000-000000000000"
HOST = "dobby"


@pytest.fixture(autouse=True)
def _host(monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOST_NAME", HOST)


async def _records() -> list[SessionBranch]:
    async with db_session() as db:
        return list((await db.execute(select(SessionBranch))).scalars())


def _switch(env, branch: str) -> None:  # noqa: F811 — pytest fixture
    """Put SID's worktree (and its row) on *branch*, made at the pushed tip."""
    _git("checkout", "-q", "-b", branch, cwd=env["cwd"])


async def _discard(env, *, branch: str, record_host: str | None = None,  # noqa: F811 — pytest fixture
                   record_source: str = bo.SOURCE_CREATE):
    orch = await _orch(env, branch=branch)
    if record_host is not None:
        assert await bo.record_branch(session_id=SID, host=record_host, path=env["cwd"],
                                      branch=branch, source=record_source)
    facts = await orch.worktree_preflight(SID)
    res = await orch.discard_worktree(SID)
    return facts, res


def _outcome(env, branch: str, **kw):  # noqa: F811 — pytest fixture
    facts, res = asyncio.run(_discard(env, branch=branch, **kw))
    assert facts["branch"] == branch and res["branch"] == branch
    # Review Focus 3: the dialog's copy is the deletion's own answer, both ways.
    assert facts["sessionBranch"] is res["branchDeleted"]
    assert res["branchDeleted"] is (not _branch_exists(env["clone"], branch))
    return res["branchDeleted"]


# ── Discard: the gate, the record, and the preflight ───────────────────────────

def test_an_unrecorded_arbitrary_session_prefixed_branch_is_kept(env):  # noqa: F811 — pytest fixture
    _switch(env, "viberpit/foo")
    assert _outcome(env, "viberpit/foo") is False


def test_a_recorded_branch_is_deleted_and_its_record_removed(env):  # noqa: F811 — pytest fixture
    _switch(env, "viberpit/custom")
    assert _outcome(env, "viberpit/custom", record_host=HOST) is True
    assert asyncio.run(_records()) == []


def test_the_sessions_own_legacy_shape_is_deleted(env):  # noqa: F811 — pytest fixture
    assert worktrees._child_branch("feature", SID) == env["branch"]
    assert _outcome(env, env["branch"]) is True


def test_another_sessions_legacy_shaped_branch_is_kept(env):  # noqa: F811 — pytest fixture
    branch = worktrees._child_branch("feature", OTHER)
    _switch(env, branch)
    assert _outcome(env, branch) is False


def test_another_sessions_backfill_record_is_not_proof(env):  # noqa: F811 — pytest fixture
    _switch(env, "viberpit/custom")

    async def run():
        orch = await _orch(env, branch="viberpit/custom")
        await bo.record_branch(session_id=OTHER, host=HOST, path=env["cwd"],
                               branch="viberpit/custom", source=bo.SOURCE_BACKFILL)
        return await orch.worktree_preflight(SID), await orch.discard_worktree(SID)
    facts, res = asyncio.run(run())
    assert facts["sessionBranch"] is False and res["branchDeleted"] is False
    assert _branch_exists(env["clone"], "viberpit/custom")


def test_a_rocky_record_never_authorises_a_delete_on_dobby(env):  # noqa: F811 — pytest fixture
    _switch(env, "viberpit/custom")
    assert _outcome(env, "viberpit/custom", record_host="rocky") is False
    assert {r.host for r in asyncio.run(_records())} == {"rocky"}


def test_a_failed_ownership_read_keeps_even_the_legacy_shape(env, monkeypatch):  # noqa: F811 — pytest fixture
    def broken():
        raise RuntimeError("db down")
    monkeypatch.setattr(bo, "db_session", broken)
    assert _outcome(env, env["branch"]) is False


def test_a_git_failure_after_removal_is_not_reported_as_a_deletion(env, monkeypatch):  # noqa: F811 — pytest fixture
    """branchDeleted, and forgetting the record, need git's PROOF of absence: a branch
    whose existence cannot be read after the removal is neither."""
    _switch(env, "viberpit/custom")
    monkeypatch.setattr(worktrees, "branch_presence", lambda clone, branch: None)
    facts, res = asyncio.run(_discard(env, branch="viberpit/custom", record_host=HOST))
    assert facts["sessionBranch"] is True
    assert res["branchDeleted"] is False
    assert {r.branch for r in asyncio.run(_records())} == {"viberpit/custom"}


@pytest.mark.asyncio
async def test_settle_keeps_the_record_when_git_cannot_read_the_clone(
        isolated_db, tmp_path):
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    await bo.record_branch(session_id=SID, host=HOST, path=str(clone), branch="viberpit/x",
                           source=bo.SOURCE_CREATE)
    gate = await bo.owned_branch_predicate(HOST, str(clone), SID)
    assert gate("viberpit/x") is True
    os.rename(clone / ".git", clone / ".git-away")      # git now fails (exit 128)
    assert await gate.settle() == []
    await bo.forget_if_gone(HOST, str(clone), "viberpit/x")
    os.rename(clone / ".git-away", clone / ".git")
    assert {r.branch for r in await _records()} == {"viberpit/x"}


def test_a_gate_whose_predicate_raises_owns_nothing():
    def boom(_b: str) -> bool:
        raise RuntimeError("db down")
    gate = bo.OwnedBranchGate(HOST, None, boom)
    assert gate.owns("viberpit/x") is False
    assert gate("viberpit/x") is False and gate.approved == []


def test_the_janitor_maps_a_shared_cwd_to_no_session(tmp_path):
    """Two rows on one cwd: neither session's backfill record or legacy shape can
    vouch for the branch there — only a creation record could."""
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    key = bo.clone_key(str(clone))
    assert key is not None
    records = {key: frozenset({bo.BranchRecord("viberpit/x", bo.SOURCE_BACKFILL, SID)})}
    facts = bo._SessionFacts(SID, None, "viberpit/x")
    alone = bo.JanitorOwnership(HOST, records, [(str(clone), facts)])
    assert alone.owned_branch_for(str(clone))("viberpit/x") is True
    # SID's row last, so a "last row wins" mapping would wrongly pick SID.
    shared = bo.JanitorOwnership(HOST, records, [
        (str(clone), bo._SessionFacts(OTHER, None, "viberpit/x")), (str(clone), facts)])
    assert shared.owned_branch_for(str(clone))("viberpit/x") is False
    own = worktrees._child_branch("main", SID)
    assert shared.owned_branch_for(str(clone))(own) is False   # no legacy rule either


# ── close / spawn rollback / fork rollback ─────────────────────────────────────

def test_close_cleanup_deletes_a_recorded_branch_and_forgets_it(env):  # noqa: F811 — pytest fixture
    _switch(env, "viberpit/custom")

    async def run():
        orch = await _orch(env, branch="viberpit/custom")
        await bo.record_branch(session_id=SID, host=HOST, path=env["cwd"],
                               branch="viberpit/custom", source=bo.SOURCE_CREATE)
        await orch._cleanup_session_paths(SID, orch._records[SID])
        return await _records()
    assert asyncio.run(run()) == []
    assert not Path(env["cwd"]).exists()
    assert not _branch_exists(env["clone"], "viberpit/custom")


def test_close_cleanup_keeps_an_unrecorded_branch(env):  # noqa: F811 — pytest fixture
    _switch(env, "viberpit/foo")

    async def run():
        orch = await _orch(env, branch="viberpit/foo")
        await orch._cleanup_session_paths(SID, orch._records[SID])
    asyncio.run(run())
    assert not Path(env["cwd"]).exists()
    assert _branch_exists(env["clone"], "viberpit/foo")


# ── transfer finalize ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_finalize_deletes_a_recorded_branch_and_forgets_it(
        monkeypatch, two_hosts, visibility) -> None:  # noqa: F811 — pytest fixture
    h = two_hosts
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")
    o = await _source(monkeypatch, h)
    branch = worktrees._child_branch("main", MOVE_SID)
    await bo.record_branch(session_id=MOVE_SID, host="dobby", path=str(h.cwd),
                           branch=branch, source=bo.SOURCE_CREATE)
    # Not the session's own shape any more: only the record can prove it.
    renamed = "viberpit/moved-away"
    _git("branch", "-m", branch, renamed, cwd=h.cwd)
    async with db_session() as db:
        await db.execute(update(SessionBranch).values(branch=renamed))
    manifest = await o.transfer_export(MOVE_SID, target="rocky", carry_dirty=False)
    async with db_session() as db:
        await db.execute(update(DbSession).where(DbSession.id == MOVE_SID)
                         .values(host="rocky"))
    out = await o.transfer_finalize(MOVE_SID, manifest, owner="rocky")
    assert out["removed"] is True
    assert not worktrees.local_branch_exists(str(h.a_clone), renamed)
    assert await _records() == []


@pytest.mark.asyncio
async def test_finalize_keeps_an_unrecorded_branch(monkeypatch, two_hosts,  # noqa: F811
                                                   visibility) -> None:  # noqa: F811 — pytest fixture
    h = two_hosts
    o = await _source(monkeypatch, h)
    _git("branch", "-m", worktrees._child_branch("main", MOVE_SID), "viberpit/foo",
         cwd=h.cwd)
    manifest = await o.transfer_export(MOVE_SID, target="rocky", carry_dirty=False)
    async with db_session() as db:
        await db.execute(update(DbSession).where(DbSession.id == MOVE_SID)
                         .values(host="rocky"))
    out = await o.transfer_finalize(MOVE_SID, manifest, owner="rocky")
    assert out["removed"] is True
    assert worktrees.local_branch_exists(str(h.a_clone), "viberpit/foo")


async def _abandoned_import(monkeypatch, h, *, branch: str, target_had_it: bool):
    """A move import that fails after import_ (the resume never comes up): _abandon
    finalizes the target tree it made."""
    from unittest.mock import AsyncMock
    from server.sessions import session_move as sm
    _git("branch", "-m", worktrees._child_branch("main", MOVE_SID), branch, cwd=h.cwd)
    if target_had_it:
        subprocess.run(["git", "-C", str(h.b_clone), "branch", branch], check=True)
    manifest = _manifest(h)
    o = await _target(monkeypatch, h)
    o.ensure_driver = AsyncMock(return_value=None)
    with pytest.raises(sm.MoveError):
        await o.transfer_import(MOVE_SID, manifest, source="dobby")
    assert not h.b_cwd.exists()
    return worktrees.local_branch_exists(str(h.b_clone), branch)


@pytest.mark.asyncio
async def test_an_abandoned_import_deletes_the_custom_prefix_branch_it_created(
        monkeypatch, two_hosts) -> None:  # noqa: F811 — pytest fixture
    """No record exists yet (it is written only after a successful import), and a
    custom-prefix name has no legacy shape: only the just-created exception can undo it."""
    assert await _abandoned_import(monkeypatch, two_hosts, branch="agent/main-5e55a0aa",
                                   target_had_it=False) is False


@pytest.mark.asyncio
async def test_an_abandoned_import_keeps_a_branch_the_target_already_had(
        monkeypatch, two_hosts) -> None:  # noqa: F811 — pytest fixture
    assert await _abandoned_import(monkeypatch, two_hosts, branch="agent/main-5e55a0aa",
                                   target_had_it=True) is True


# ── Discard's pre-removal snapshot asks the same ownership predicate ───────────

def test_discard_snapshots_the_tip_of_an_owned_custom_prefix_branch(env):  # noqa: F811 — pytest fixture
    """An unpushed commit on a recorded custom-prefix branch is kept as a recovery ref
    BEFORE anything is removed — the snapshot asks ownership, not the name."""
    _switch(env, "agent/custom")
    Path(env["cwd"], "w.txt").write_text("unpushed\n")
    _git("add", "-A", cwd=env["cwd"])
    _git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "w", cwd=env["cwd"])
    tip = _git("rev-parse", "HEAD", cwd=env["cwd"])
    calls: list[list[str] | None] = []
    real = worktrees.snapshot_before_discard

    def spy(*a, **k):
        out = real(*a, **k)
        calls.append(out)
        return out
    with patch.object(worktrees, "snapshot_before_discard", spy):
        facts, res = asyncio.run(_discard(env, branch="agent/custom", record_host=HOST))
    assert res["branchDeleted"] is True
    (snap,) = calls
    assert snap and any(_git("rev-parse", r, cwd=env["clone"]) == tip for r in snap)


# ── the janitor: one snapshot per sweep, and the unmapped-worktree rule ────────

def _run(*args: str, cwd) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def janitor(tmp_path, isolated_db):
    root = tmp_path / "root"
    root.mkdir()
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    _run("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "--allow-empty",
         "-m", "init", cwd=seed)
    _run("remote", "add", "origin", str(origin), cwd=seed)
    _run("push", "-q", "origin", "main", cwd=seed)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    return root, clone


def _stale_worktree(root: Path, clone: Path, sid: str, branch: str) -> Path:
    """A clean, fully pushed, day-old managed worktree on *branch*."""
    dest = worktrees.session_worktree_dir(str(root), str(clone), sid)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run("worktree", "add", "-q", "-b", branch, str(dest), "origin/main", cwd=clone)
    old = time.time() - 3 * 86400
    os.utime(dest, (old, old))
    return dest


@pytest.mark.asyncio
async def test_janitor_reads_once_per_sweep_and_gates_every_removal(janitor, monkeypatch):
    root, clone = janitor
    s1, s2, s3, s4, s5 = (f"{c * 8}-0000-0000-0000-000000000000" for c in "12345")
    own = worktrees._child_branch("main", s1)                 # s1's own legacy shape
    wt1 = _stale_worktree(root, clone, s1, own)
    wt2 = _stale_worktree(root, clone, s2, "viberpit/made-here")      # unmapped, created
    wt3 = _stale_worktree(root, clone, s3, worktrees._child_branch("main", s3))  # unmapped
    wt4 = _stale_worktree(root, clone, s4, "viberpit/old-one")        # unmapped, backfill
    wt5 = _stale_worktree(root, clone, s5, worktrees._child_branch("main", OTHER))
    async with db_session() as db:
        for sid, wt, branch in ((s1, wt1, own),
                                (s5, wt5, worktrees._child_branch("main", OTHER))):
            db.add(DbSession(id=sid, name="s", account_name="a", status="ended",
                             cwd=str(wt), branch=branch))
    await bo.record_branch(session_id=s2, host=HOST, path=str(clone),
                           branch="viberpit/made-here", source=bo.SOURCE_CREATE)
    await bo.record_branch(session_id=s4, host=HOST, path=str(clone),
                           branch="viberpit/old-one", source=bo.SOURCE_BACKFILL)

    orch = SessionOrchestrator(repo_root=str(root))
    snapshots: list[int] = []
    real_snapshot, real_db = bo.janitor_ownership, bo.db_session
    in_thread = {"db": 0}
    sweeping = {"on": False}

    async def counted(host):
        snapshots.append(1)
        return await real_snapshot(host)

    def watched_db():
        if sweeping["on"]:
            in_thread["db"] += 1
        return real_db()
    monkeypatch.setattr(bo, "janitor_ownership", counted)
    monkeypatch.setattr(bo, "db_session", watched_db)
    real_sweep = orch._janitor_sweep_once

    def sweep(*a):
        sweeping["on"] = True
        try:
            return real_sweep(*a)
        finally:
            sweeping["on"] = False
    orch._janitor_sweep_once = sweep  # type: ignore[method-assign]

    await orch._janitor_tick()

    assert snapshots == [1] and in_thread["db"] == 0       # one read, none in the thread
    assert not any(p.exists() for p in (wt1, wt2, wt3, wt4, wt5))  # every tree reclaimed
    exists = {b: worktrees.local_branch_exists(str(clone), b) for b in (
        own, "viberpit/made-here", worktrees._child_branch("main", s3), "viberpit/old-one",
        worktrees._child_branch("main", OTHER))}
    assert exists == {
        own: False,                                       # mapped: own legacy shape
        "viberpit/made-here": False,                      # unmapped: creation record
        worktrees._child_branch("main", s3): True,        # unmapped: legacy needs a session
        "viberpit/old-one": True,                         # unmapped: backfill needs one too
        worktrees._child_branch("main", OTHER): True,     # mapped, another session's shape
    }
    # The deleted branch's record is forgotten; the kept one's stays.
    assert {r.branch for r in await _records()} == {"viberpit/old-one"}


@pytest.mark.asyncio
async def test_a_failed_janitor_snapshot_keeps_every_branch(janitor, monkeypatch):
    root, clone = janitor
    s1 = "11111111-0000-0000-0000-000000000000"
    own = worktrees._child_branch("main", s1)
    wt1 = _stale_worktree(root, clone, s1, own)
    async with db_session() as db:
        db.add(DbSession(id=s1, name="s", account_name="a", status="ended", cwd=str(wt1),
                         branch=own))

    def broken():
        raise RuntimeError("db down")
    monkeypatch.setattr(bo, "db_session", broken)
    await SessionOrchestrator(repo_root=str(root))._janitor_tick()
    assert not wt1.exists() and worktrees.local_branch_exists(str(clone), own)


# ── D-22 choice 3A: kept branches are surfaced, read-only ──────────────────────

@pytest.mark.parametrize("branch,prefix,record,broken,want", [
    (None, None, False, False, ("owned", True)),             # own legacy shape
    ("viberpit/foo", None, False, False, ("unowned", True)),
    ("viberpit/foo", None, True, False, ("owned", True)),
    ("viberpit/foo", None, False, True, ("unknown", True)),   # the read failed
    ("mine", None, False, False, ("unowned", False)),        # the user's own branch
    ("agent/x", "agent/", False, False, ("unowned", True)),  # the configured prefix
])
def test_preflight_reports_branch_ownership(
        env, monkeypatch, branch, prefix, record, broken, want):  # noqa: F811 — pytest fixture
    from server.db.models import AppSettings
    if branch is not None:
        _switch(env, branch)
    branch = branch or env["branch"]

    async def run():
        orch = await _orch(env, branch=branch)
        if prefix:
            async with db_session() as db:
                db.add(AppSettings(id="singleton", branch_prefix=prefix))
        if record:
            await bo.record_branch(session_id=SID, host=HOST, path=env["cwd"],
                                   branch=branch, source=bo.SOURCE_CREATE)
        if broken:
            def down():
                raise RuntimeError("db down")
            monkeypatch.setattr(bo, "db_session", down)
        return await orch.worktree_preflight(SID)
    facts = asyncio.run(run())
    assert (facts["branchOwnership"], facts["branchPrefixed"]) == want
    assert facts["sessionBranch"] is (want[0] == "owned")


def _kept_records(caplog):
    return [r for r in caplog.records if r.getMessage() == "worktree.janitor.kept_unowned"]


@pytest.mark.asyncio
async def test_janitor_summarises_kept_branches_once_a_week(janitor, monkeypatch, caplog):
    import logging
    from server.obs.logdedupe import LogThrottle
    monkeypatch.setattr(bo, "_KEPT_THROTTLE", LogThrottle(window_s=7 * 86400.0))
    root, clone = janitor
    s3 = "33333333-0000-0000-0000-000000000000"
    _stale_worktree(root, clone, s3, worktrees._child_branch("main", s3))   # unmapped
    orch = SessionOrchestrator(repo_root=str(root))
    with caplog.at_level(logging.DEBUG, logger=bo.log.name):
        await orch._janitor_tick()
    (rec,) = _kept_records(caplog)
    assert rec.levelno == logging.INFO and rec.fields == {"count": 1, "unknown": 0}
    caplog.clear()
    s4 = "44444444-0000-0000-0000-000000000000"
    _stale_worktree(root, clone, s4, worktrees._child_branch("main", s4))
    with caplog.at_level(logging.DEBUG, logger=bo.log.name):
        await orch._janitor_tick()
    (again,) = _kept_records(caplog)
    assert again.levelno == logging.DEBUG                   # throttled within the week


@pytest.mark.asyncio
async def test_janitor_summary_counts_unknown_ownership(janitor, monkeypatch, caplog):
    import logging
    from server.obs.logdedupe import LogThrottle
    monkeypatch.setattr(bo, "_KEPT_THROTTLE", LogThrottle(window_s=7 * 86400.0))
    root, clone = janitor
    s1 = "11111111-0000-0000-0000-000000000000"
    _stale_worktree(root, clone, s1, worktrees._child_branch("main", s1))

    def broken():
        raise RuntimeError("db down")
    monkeypatch.setattr(bo, "db_session", broken)
    with caplog.at_level(logging.INFO, logger=bo.log.name):
        await SessionOrchestrator(repo_root=str(root))._janitor_tick()
    (rec,) = _kept_records(caplog)
    assert rec.fields == {"count": 1, "unknown": 1}


# ── every remove_worktree call site passes a predicate ─────────────────────────

_GATED = {"remove_worktree": "owned_branch", "sweep_orphaned_worktrees": "owned_branch_for"}
_REPO = Path(__file__).resolve().parents[2]


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _ungated_calls(source: str) -> tuple[int, list[int]]:
    """``(gated calls seen, lines of calls missing the keyword)`` — a call of a gated
    function, or a call handing one over as its first argument (``partial``,
    ``to_thread``), must carry the keyword itself. ``wtx.finalize`` likewise."""
    seen, bad = 0, []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        target = _name(node.func)
        if not (target in _GATED or target == "finalize"):
            target = _name(node.args[0]) if node.args else None
        if target == "finalize" and not (
                isinstance(node.func, ast.Attribute) and _name(node.func.value) == "wtx"
                or node.args and isinstance(node.args[0], ast.Attribute)
                and _name(node.args[0].value) == "wtx"):
            continue
        kw = _GATED.get(target or "") or ("owned_branch" if target == "finalize" else None)
        if kw is None:
            continue
        seen += 1
        if kw not in {k.arg for k in node.keywords}:
            bad.append(node.lineno)
    return seen, bad


def test_every_call_site_passes_an_ownership_predicate():
    files = [p for d in ("src", "server") for p in (_REPO / d).rglob("*.py")
             if "tests" not in p.parts]
    seen, offenders = 0, []
    for p in files:
        n, bad = _ungated_calls(p.read_text())
        seen += n
        offenders += [f"{p.relative_to(_REPO)}:{line}" for line in bad]
    assert not offenders
    # Discard, close/rollback, source + abandoned-import finalize, the janitor's two
    # removals and its sweep call, and finalize's own remove: the scan found them.
    assert seen >= 8


def test_the_scan_can_fail():
    assert _ungated_calls("worktrees.remove_worktree(p)") == (1, [1])
    assert _ungated_calls("asyncio.to_thread(worktrees.remove_worktree, p, True)") == (1, [1])
    assert _ungated_calls("functools.partial(wtx.finalize, b, c)") == (1, [1])
    assert _ungated_calls("remove_worktree(p, owned_branch=f)") == (1, [])
    assert _ungated_calls("jobs.finalize(job, 'x')") == (0, [])
