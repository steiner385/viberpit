"""The branch ownership registry (D-22, Task 1): ViberPit records every branch it
creates, so a later ``git branch -D`` can require that proof instead of a name match.

Plan: docs/superpowers/plans/2026-11-06-desktop-parity-d22-branch-prefix.md, Task 1.
Review Focus 1 (only real creations are recorded; a failed write fails closed),
2 (the user's own branch is never recorded) and 5 (records are host- and
clone-scoped, and survive a move). Real temp git repos, a real orchestrator spawn
and fork path on a fake SDK, and a fresh DB per test.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from viberpit import brand, worktrees
from server.db import db_session
from server.db.models import DataBackfill, Session as DbSession, SessionBranch
from server.sessions import branch_ownership as bo
# Fixtures and helpers reused from the fork and move suites (real git, fake SDK).
from server.tests.test_session_fork import _fork, _git as _fork_git, env  # noqa: F401
from server.tests.test_session_move import (  # noqa: F401
    SID as MOVE_SID, _managed, _manifest, _target, two_hosts,
)

SID = "abcd1234-5678-0000-0000-000000000000"
OTHER = "eeee9999-5678-0000-0000-000000000000"
PARENT = "f00dcafe-1111-2222-3333-444455556666"
#: The pre-rebrand branch prefix, from its one source (brand) rather than spelled here.
LEGACY = brand.LEGACY_BRANCH_PREFIX


async def _records() -> list[SessionBranch]:
    async with db_session() as db:
        return list((await db.execute(select(SessionBranch))).scalars())


# ── the strict legacy shape rule ───────────────────────────────────────────────

FORK1 = "viberpit/fork-f00dcafe-1"


@pytest.mark.parametrize("branch,fork_parent,row_branch,want", [
    ("viberpit/main-abcd1234", None, None, True),         # this session's child shape
    (f"{LEGACY}main-abcd1234", None, None, True),         # legacy spelling, same shape
    ("viberpit/feat/x-abcd1234", None, None, True),       # a base with a slash
    ("viberpit/main-eeee9999", None, None, False),        # ANOTHER session's shape
    ("viberpit/-abcd1234", None, None, False),            # no base
    ("viberpit/abcd1234", None, None, False),             # no separator
    ("main-abcd1234", None, None, False),                 # no built-in prefix
    ("feature/main-abcd1234", None, None, False),         # a user namespace
    ("viberpit/foo", None, None, False),                  # arbitrary name under the prefix
    (FORK1, PARENT, FORK1, True),                         # a fork child's own branch
    (f"{LEGACY}fork-f00dcafe-12", PARENT, f"{LEGACY}fork-f00dcafe-12", True),
    # Only the fork's OWN n: a sibling fork of the same source is not its branch, and
    # with no recorded row branch there is no telling which n is its — refused.
    ("viberpit/fork-f00dcafe-2", PARENT, FORK1, False),
    (FORK1, PARENT, None, False),
    ("viberpit/fork-f00dcafe-0", PARENT, "viberpit/fork-f00dcafe-0", False),  # n ≥ 1
    ("viberpit/fork-f00dcafe-01", PARENT, "viberpit/fork-f00dcafe-01", False),
    ("viberpit/fork-f00dcafe-x", PARENT, "viberpit/fork-f00dcafe-x", False),
    ("viberpit/fork-eeee9999-1", PARENT, "viberpit/fork-eeee9999-1", False),  # other source
    ("viberpit/main-abcd1234", PARENT, "viberpit/main-abcd1234", False),  # fork: no child
    # A non-fork row never owns a fork-shaped branch — not even one cut from it:
    # deleting a source must never take its forks' branches with it.
    ("viberpit/fork-abcd1234-1", None, "viberpit/fork-abcd1234-1", False),
])
def test_legacy_shape_is_strict(branch, fork_parent, row_branch, want):
    assert bo.legacy_session_branch(branch, SID, fork_parent_id=fork_parent,
                                    row_branch=row_branch) is want


def test_legacy_shape_matches_the_name_builders():
    """The rule is derived from the builders, so the two cannot drift."""
    assert bo.legacy_session_branch(worktrees._child_branch("main", SID), SID)
    fork = worktrees.fork_branch_name(PARENT, 3)
    assert bo.legacy_session_branch(fork, OTHER, fork_parent_id=PARENT, row_branch=fork)


# ── the one clone key ──────────────────────────────────────────────────────────

def _repo(path: Path, *branches: str) -> Path:
    """A git repo with one commit and *branches* created at it."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@e",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    for b in branches:
        subprocess.run(["git", "-C", str(path), "branch", b], check=True)
    return path


def test_clone_key_is_the_common_dir_top_for_every_path_in_the_repo(tmp_path):
    clone = _repo(tmp_path / "clone")
    (clone / "sub").mkdir()
    wt = tmp_path / "wt-parent"
    subprocess.run(["git", "-C", str(clone), "worktree", "add", "-q", "-b", "p", str(wt)],
                   check=True)
    link = tmp_path / "link"
    link.symlink_to(clone)
    key = os.path.realpath(clone)
    # A linked worktree keys to its clone, never to itself (--show-toplevel would).
    for path in (clone, clone / "sub", wt, link):
        assert bo.clone_key(str(path)) == key
    assert bo.clone_key(str(tmp_path)) is None           # not a repo


# ── record / lookup / predicate ────────────────────────────────────────────────

def _rec(branch: str, source: str = bo.SOURCE_CREATE, sid: str = SID) -> bo.BranchRecord:
    return bo.BranchRecord(branch, source, sid)


@pytest.mark.asyncio
async def test_record_is_idempotent_and_canonicalises_the_clone(isolated_db, tmp_path):
    clone = _repo(tmp_path / "clone")
    link = tmp_path / "link"
    link.symlink_to(clone)
    assert await bo.record_branch(session_id=SID, host="dobby", path=str(link),
                                  branch="viberpit/main-abcd1234", source=bo.SOURCE_CREATE)
    assert await bo.record_branch(session_id=SID, host="dobby", path=str(clone),
                                  branch="viberpit/main-abcd1234", source=bo.SOURCE_CREATE)
    (rec,) = await _records()
    assert (rec.session_id, rec.host, rec.clone, rec.branch, rec.source) == (
        SID, "dobby", os.path.realpath(clone), "viberpit/main-abcd1234", "create")
    assert rec.created_at is not None and rec.created_at.tzinfo is None   # naive UTC
    assert await bo.branch_records("dobby", str(link)) == frozenset(
        {_rec("viberpit/main-abcd1234")})


@pytest.mark.asyncio
async def test_records_are_host_and_clone_scoped(isolated_db, tmp_path):
    a, b = _repo(tmp_path / "a"), _repo(tmp_path / "b")
    # Not this session's legacy shape, so only the record can make it owned.
    branch = "viberpit/custom-name"
    await bo.record_branch(session_id=SID, host="rocky", path=str(a), branch=branch,
                           source=bo.SOURCE_CREATE)
    assert (await bo.owned_branch_predicate("rocky", str(a), SID))(branch) is True
    assert (await bo.owned_branch_predicate("dobby", str(a), SID))(branch) is False
    assert (await bo.owned_branch_predicate("rocky", str(b), SID))(branch) is False
    assert await bo.branch_records("dobby", str(a)) == frozenset()


@pytest.mark.asyncio
async def test_predicate_applies_the_legacy_rule_for_the_sessions_own_row(isolated_db,
                                                                          tmp_path):
    clone = _repo(tmp_path / "clone")
    async with db_session() as db:
        db.add(DbSession(id=OTHER, name="f", account_name="a", cwd="/x",
                         parent_session_id=PARENT, is_fork=True,
                         branch="viberpit/fork-f00dcafe-2"))
    owned = await bo.owned_branch_predicate("dobby", str(clone), SID)
    assert owned("viberpit/main-abcd1234") is True       # own shape, no record
    assert owned("viberpit/main-eeee9999") is False      # another session's shape
    assert owned("viberpit/foo") is False                # arbitrary, unrecorded
    assert owned("feature") is False
    fork_owned = await bo.owned_branch_predicate("dobby", str(clone), OTHER)
    assert fork_owned("viberpit/fork-f00dcafe-2") is True    # its own recorded n
    assert fork_owned("viberpit/fork-f00dcafe-1") is False   # a sibling fork's
    assert fork_owned("viberpit/main-eeee9999") is False


def test_predicate_from_an_unknown_snapshot_keeps_everything():
    owned = bo.predicate_from(None, SID)
    assert owned("viberpit/main-abcd1234") is False
    assert bo.predicate_from(frozenset({_rec("x")}), SID)("x") is True


@pytest.mark.parametrize("source", [bo.SOURCE_CREATE, bo.SOURCE_FORK, bo.SOURCE_IMPORT])
def test_a_creation_record_is_owned_whichever_session_is_removed(source):
    records = frozenset({_rec("x", source, sid=SID)})
    assert bo.predicate_from(records, OTHER)("x") is True


def test_a_backfill_record_only_vouches_for_its_own_session():
    records = frozenset({_rec("viberpit/x", bo.SOURCE_BACKFILL, sid=SID)})
    assert bo.predicate_from(records, SID)("viberpit/x") is True
    assert bo.predicate_from(records, OTHER)("viberpit/x") is False


@pytest.mark.asyncio
async def test_a_backfill_record_does_not_authorise_another_session(isolated_db, tmp_path):
    clone = _repo(tmp_path / "clone")
    await bo.record_branch(session_id=SID, host="dobby", path=str(clone),
                           branch="viberpit/x", source=bo.SOURCE_BACKFILL)
    assert (await bo.owned_branch_predicate("dobby", str(clone), SID))("viberpit/x")
    assert not (await bo.owned_branch_predicate("dobby", str(clone), OTHER))("viberpit/x")


@pytest.mark.asyncio
async def test_a_failed_write_fails_closed_and_warns(isolated_db, tmp_path, monkeypatch,
                                                     caplog):
    clone = _repo(tmp_path / "clone")

    def broken():
        raise RuntimeError("db down")
    monkeypatch.setattr(bo, "db_session", broken)
    with caplog.at_level(logging.WARNING, logger=bo.log.name):
        ok = await bo.record_branch(session_id=SID, host="dobby", path=str(clone),
                                    branch="viberpit/main-abcd1234",
                                    source=bo.SOURCE_CREATE)
    assert ok is False
    (rec,) = [r for r in caplog.records if r.name == bo.log.name]
    assert rec.levelno == logging.WARNING
    assert rec.getMessage() == "session_branch.record_failed"     # bare event name
    monkeypatch.undo()
    assert await _records() == []


@pytest.mark.asyncio
async def test_an_unresolvable_clone_fails_closed_and_warns(isolated_db, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger=bo.log.name):
        assert await bo.record_branch(session_id=SID, host="dobby", path=str(tmp_path),
                                      branch="viberpit/x", source=bo.SOURCE_CREATE) is False
    assert await _records() == []
    assert any(r.getMessage() == "session_branch.record_failed" for r in caplog.records)
    assert await bo.branch_records("dobby", str(tmp_path)) is None
    assert (await bo.owned_branch_predicate("dobby", str(tmp_path), SID))(
        "viberpit/main-abcd1234") is False


@pytest.mark.asyncio
async def test_a_failed_read_keeps_every_branch(isolated_db, tmp_path, monkeypatch):
    clone = _repo(tmp_path / "clone")

    def broken():
        raise RuntimeError("db down")
    monkeypatch.setattr(bo, "db_session", broken)
    assert await bo.branch_records("dobby", str(clone)) is None
    owned = await bo.owned_branch_predicate("dobby", str(clone), SID)
    assert owned("viberpit/main-abcd1234") is False      # even the legacy shape


# ── recording at spawn and fork (real orchestrator, real git) ──────────────────

def _host() -> str:
    from server.hostname import self_host_name
    return self_host_name()


@pytest.mark.asyncio
async def test_a_spawned_child_branch_is_recorded(env) -> None:  # noqa: F811 — pytest fixture
    orch, _spawns, _src, _root, clone = env
    assert await _records() == []          # the detached source made no branch
    # `main` is checked out in the clone and there is no origin: path 3, a child branch.
    rec = await orch.create_session("Child", cwd=str(clone), branch="main")
    assert rec.branch == worktrees._child_branch("main", rec.id)
    (row,) = await _records()
    assert (row.session_id, row.host, row.clone, row.branch, row.source) == (
        rec.id, _host(), os.path.realpath(clone), rec.branch, bo.SOURCE_CREATE)


@pytest.mark.asyncio
async def test_a_spawn_from_a_linked_worktree_records_under_the_clone(
        env, tmp_path) -> None:  # noqa: F811 — pytest fixture
    """An adopted parent's cwd can be a linked worktree: the record is keyed by the
    common-dir top (the clone), never the worktree --show-toplevel reports."""
    orch, _spawns, _src, _root, clone = env
    wt = tmp_path / "wt-parent"
    _fork_git(clone, "worktree", "add", "-q", "-b", "parent-branch", str(wt))
    rec = await orch.create_session("Child", cwd=str(wt), branch="main")
    assert rec.branch == worktrees._child_branch("main", rec.id)
    (row,) = await _records()
    assert row.clone == os.path.realpath(clone) != os.path.realpath(wt)
    assert row.branch == rec.branch and row.source == bo.SOURCE_CREATE


@pytest.mark.asyncio
async def test_the_users_own_branch_is_never_recorded(env) -> None:  # noqa: F811 — pytest fixture
    orch, _spawns, _src, _root, clone = env
    _fork_git(clone, "branch", "feature")
    # `feature` is free: path 2 checks out the user's own branch, no child.
    rec = await orch.create_session("Own", cwd=str(clone), branch="feature")
    assert rec.branch == "feature"
    assert await _records() == []


@pytest.mark.asyncio
async def test_a_fork_branch_is_recorded_for_the_child(env) -> None:  # noqa: F811 — pytest fixture
    orch, _spawns, src, _root, clone = env
    out = await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    child = orch.record(out["sessionId"])
    assert child.branch == worktrees.fork_branch_name(src.id, 1)
    (row,) = await _records()
    assert (row.session_id, row.host, row.clone, row.branch, row.source) == (
        child.id, _host(), os.path.realpath(clone), child.branch, bo.SOURCE_FORK)


@pytest.mark.asyncio
async def test_a_spawn_whose_record_write_fails_still_starts_unrecorded(
        env, monkeypatch, caplog) -> None:  # noqa: F811 — pytest fixture
    orch, _spawns, _src, _root, clone = env

    def broken():
        raise RuntimeError("db down")
    monkeypatch.setattr(bo, "db_session", broken)
    with caplog.at_level(logging.WARNING, logger=bo.log.name):
        rec = await orch.create_session("Child", cwd=str(clone), branch="main")
    monkeypatch.undo()
    assert worktrees.local_branch_exists(str(clone), rec.branch)
    assert await _records() == []          # fail closed: never auto-deletable
    assert any(r.getMessage() == "session_branch.record_failed"
               and r.levelno == logging.WARNING for r in caplog.records)


# ── rollbacks forget what they deleted (D-22 Task 2) ───────────────────────────

@pytest.mark.asyncio
async def test_a_spawn_rollback_forgets_the_record_of_the_branch_it_deleted(
        env, monkeypatch) -> None:  # noqa: F811 — pytest fixture
    orch, _spawns, _src, _root, clone = env
    import server.sessions.orchestrator as mod

    def busy(*a, **k):
        raise RuntimeError("all accounts busy")
    monkeypatch.setattr(mod, "select_account", busy)
    with pytest.raises(RuntimeError):
        await orch.create_session("Child", cwd=str(clone), branch="main")
    assert await _records() == []
    assert _fork_git(clone, "branch", "--list", "viberpit/main-*").strip() == ""


@pytest.mark.asyncio
async def test_a_fork_rollback_forgets_the_fork_branch_record(env) -> None:  # noqa: F811 — pytest fixture
    orch, spawns, src, _root, clone = env
    spawns.fail_fork = True
    with pytest.raises(Exception):
        await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    assert await _records() == []
    assert _fork_git(clone, "branch", "--list", "*fork-*").strip() == ""


@pytest.mark.asyncio
async def test_a_failed_prespawn_rollback_keeps_the_users_own_branch(
        env, monkeypatch) -> None:  # noqa: F811 — pytest fixture
    """Path 2 checks out the USER's branch (is_child False): a spawn that fails after the
    worktree exists removes the tree but never takes the user's branch with it."""
    orch, _spawns, _src, _root, clone = env
    _fork_git(clone, "branch", "feature")
    import server.sessions.orchestrator as mod

    def busy(*a, **k):
        raise RuntimeError("all accounts busy")
    monkeypatch.setattr(mod, "select_account", busy)
    with pytest.raises(RuntimeError):
        await orch.create_session("Own", cwd=str(clone), branch="feature")
    assert worktrees.local_branch_exists(str(clone), "feature")


@pytest.mark.asyncio
async def test_a_failed_driver_start_rollback_keeps_the_users_own_branch(
        env) -> None:  # noqa: F811 — pytest fixture
    """The driver-rollback site (after the record is pre-seeded) has the same guard."""
    orch, spawns, _src, _root, clone = env
    _fork_git(clone, "branch", "feature")

    def failing(*a, **k):
        client = spawns(*a, **k)

        async def boom():
            raise RuntimeError("cli exited")
        client.__aenter__ = boom  # type: ignore[method-assign]
        return client
    orch._sdk_factory = failing
    with pytest.raises(Exception):
        await orch.create_session("Own", cwd=str(clone), branch="feature")
    assert worktrees.local_branch_exists(str(clone), "feature")
    # ...while the same failure on a child branch still deletes what the spawn made.
    with pytest.raises(Exception):
        await orch.create_session("Child", cwd=str(clone), branch="main")
    assert _fork_git(clone, "branch", "--list", "viberpit/main-*").strip() == ""


# ── recording at move import ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_move_import_carries_the_record(monkeypatch, two_hosts) -> None:  # noqa: F811 — pytest fixture
    h = two_hosts
    branch = worktrees._child_branch("main", MOVE_SID)
    await bo.record_branch(session_id=MOVE_SID, host="dobby", path=str(h.a_clone),
                           branch=branch, source=bo.SOURCE_CREATE)
    manifest = _manifest(h)
    o = await _target(monkeypatch, h)
    await o.transfer_import(MOVE_SID, manifest, source="dobby")
    rows = {(r.host, r.clone, r.branch, r.source, r.session_id) for r in await _records()}
    assert rows == {
        ("dobby", os.path.realpath(h.a_clone), branch, "create", MOVE_SID),
        ("rocky", os.path.realpath(h.b_clone), branch, "import", MOVE_SID),
    }


@pytest.mark.asyncio
async def test_a_move_from_the_primary_matches_the_name_it_recorded_under(
        monkeypatch, two_hosts) -> None:  # noqa: F811 — pytest fixture
    """The fleet routes a primary's NULL stamp as its registry name or "local", but the
    primary recorded under its self_host_name() — which its export manifest carries."""
    h = two_hosts
    branch = worktrees._child_branch("main", MOVE_SID)
    await bo.record_branch(session_id=MOVE_SID, host="dobby", path=str(h.a_clone),
                           branch=branch, source=bo.SOURCE_CREATE)
    manifest = {**_manifest(h), "source": "dobby"}     # what transfer_export returns
    o = await _target(monkeypatch, h)
    await o.transfer_import(MOVE_SID, manifest, source="local")
    assert ("rocky", "import") in {(r.host, r.source) for r in await _records()}


@pytest.mark.asyncio
async def test_a_move_import_without_a_source_record_records_nothing(
        monkeypatch, two_hosts) -> None:  # noqa: F811 — pytest fixture
    h = two_hosts
    manifest = _manifest(h)
    o = await _target(monkeypatch, h)
    await o.transfer_import(MOVE_SID, manifest, source="dobby")
    assert await _records() == []


@pytest.mark.asyncio
async def test_a_move_import_onto_an_existing_branch_records_nothing(
        monkeypatch, two_hosts) -> None:  # noqa: F811 — pytest fixture
    """The target clone already had the branch (``prev`` set): this import did not
    create it, so it is not proof of ownership on the target."""
    h = two_hosts
    branch = worktrees._child_branch("main", MOVE_SID)
    await bo.record_branch(session_id=MOVE_SID, host="dobby", path=str(h.a_clone),
                           branch=branch, source=bo.SOURCE_CREATE)
    subprocess.run(["git", "-C", str(h.b_clone), "branch", branch], check=True)
    manifest = _manifest(h)
    o = await _target(monkeypatch, h)
    await o.transfer_import(MOVE_SID, manifest, source="dobby")
    assert {r.host for r in await _records()} == {"dobby"}


# ── the one-shot backfill ──────────────────────────────────────────────────────

async def _unmark() -> None:
    """Drop the marker isolated_db's own init_db left, so the backfill runs again."""
    async with db_session() as db:
        for row in (await db.execute(select(DataBackfill))).scalars():
            if row.name.startswith("session_branches:"):
                await db.delete(row)


async def _seed(*rows: DbSession) -> None:
    async with db_session() as db:
        for row in rows:
            db.add(row)


@pytest.mark.asyncio
async def test_backfill_records_only_each_rows_own_shape(isolated_db, tmp_path,
                                                         monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")
    await _unmark()
    s8 = "88888888-0000-0000-0000-000000000000"
    # Every candidate branch exists in the clone, except s8's.
    clone = _repo(tmp_path / "clone", "viberpit/main-abcd1234", f"{LEGACY}fork-f00dcafe-1",
                  "feature", "viberpit/main-55555555", "viberpit/main-66666666",
                  "viberpit/fork-f00dcafe-1")
    s3 = "33333333-0000-0000-0000-000000000000"
    s4 = "44444444-0000-0000-0000-000000000000"
    s5 = "55555555-0000-0000-0000-000000000000"
    s6 = "66666666-0000-0000-0000-000000000000"
    s7 = "77777777-0000-0000-0000-000000000000"
    await _seed(
        # Own child shape, live cwd in the clone → recorded.
        DbSession(id=SID, name="a", account_name="a", cwd=str(clone),
                  branch="viberpit/main-abcd1234"),
        # Another session's shape → NOT recorded (it is not this row's proof).
        DbSession(id=OTHER, name="b", account_name="a", cwd=str(clone),
                  branch="viberpit/main-abcd1234"),
        # The user's own branch (path 2) → never.
        DbSession(id=s3, name="c", account_name="a", cwd=str(clone), branch="feature"),
        # A fork child's shape, legacy spelling, archived (cwd gone) → resolved via
        # the clone path from its repo.
        DbSession(id=s4, name="d", account_name="a", cwd=str(tmp_path / "gone"),
                  repo="acme/clone", branch=f"{LEGACY}fork-f00dcafe-1",
                  parent_session_id=PARENT, is_fork=True, status="archived"),
        # Unresolvable clone (no cwd on disk, no repo) → skipped.
        DbSession(id=s5, name="e", account_name="a", cwd=str(tmp_path / "gone"),
                  branch="viberpit/main-55555555"),
        # Another host's row → that host's own startup records it, never this one.
        DbSession(id=s6, name="f", account_name="a", cwd=str(clone), host="rocky",
                  branch="viberpit/main-66666666"),
        # A dispatched (non-fork) child with a parent: its own child shape only.
        DbSession(id=s7, name="g", account_name="a", cwd=str(clone),
                  branch="viberpit/fork-f00dcafe-1", parent_session_id=PARENT),
        # Its own shape and a live clone, but the branch is gone → not recorded (a
        # record for a missing branch would vouch for a later same-named one).
        DbSession(id=s8, name="h", account_name="a", cwd=str(clone),
                  branch="viberpit/main-88888888"),
    )
    monkeypatch.setattr(bo, "_clone_path_for", lambda root, repo: clone
                        if repo == "acme/clone" else None)
    n = await bo.backfill_session_branches(repo_root_fallback=str(tmp_path))
    rows = {(r.session_id, r.host, r.clone, r.branch, r.source) for r in await _records()}
    key = os.path.realpath(clone)
    assert rows == {
        (SID, "dobby", key, "viberpit/main-abcd1234", "backfill"),
        (s4, "dobby", key, f"{LEGACY}fork-f00dcafe-1", "backfill"),
    }
    assert n == 2
    async with db_session() as db:
        assert await db.get(DataBackfill, "session_branches:dobby") is not None


@pytest.mark.asyncio
async def test_backfill_is_one_shot_per_host(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")
    await _unmark()
    clone = _repo(tmp_path / "clone", "viberpit/main-abcd1234", "viberpit/main-eeee9999")
    await _seed(DbSession(id=SID, name="a", account_name="a", cwd=str(clone),
                          branch="viberpit/main-abcd1234"))
    assert await bo.backfill_session_branches(repo_root_fallback=str(tmp_path)) == 1
    await _seed(DbSession(id=OTHER, name="b", account_name="a", cwd=str(clone),
                          branch="viberpit/main-eeee9999"))
    assert await bo.backfill_session_branches(repo_root_fallback=str(tmp_path)) == 0
    assert len(await _records()) == 1
    # Concurrent startups on one host (edge + daemon) both find the marker absent: the
    # second must neither duplicate a record nor fail on the marker.
    async with db_session() as db:
        await db.delete(await db.get(DataBackfill, "session_branches:dobby"))
    results = await asyncio.gather(
        bo.backfill_session_branches(repo_root_fallback=str(tmp_path)),
        bo.backfill_session_branches(repo_root_fallback=str(tmp_path)))
    assert all(isinstance(r, int) for r in results)
    assert {r.branch for r in await _records()} == {"viberpit/main-abcd1234",
                                                     "viberpit/main-eeee9999"}


@pytest.mark.asyncio
async def test_backfill_failure_never_breaks_startup(isolated_db, tmp_path, monkeypatch,
                                                     caplog):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(bo, "_backfill", boom)
    with caplog.at_level(logging.WARNING, logger=bo.log.name):
        assert await bo.backfill_session_branches(repo_root_fallback=str(tmp_path)) == 0
    assert any(r.getMessage() == "session_branch.backfill_failed" for r in caplog.records)


@pytest.mark.asyncio
async def test_init_db_runs_the_backfill(tmp_path, monkeypatch):
    """Wired into startup: a fresh init_db leaves this host's marker behind."""
    from server.db import dispose_db, init_db
    monkeypatch.setenv("VIBERPIT_HOST_NAME", "dobby")
    url = f"sqlite+aiosqlite:///{tmp_path / 'boot.db'}"
    monkeypatch.setenv("VIBERPIT_DB_URL", url)
    await init_db(url)
    try:
        async with db_session() as db:
            assert await db.get(DataBackfill, "session_branches:dobby") is not None
    finally:
        await dispose_db()
