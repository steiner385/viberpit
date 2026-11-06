"""The configurable session-branch prefix (D-22, Task 3).

Plan: docs/superpowers/plans/2026-11-06-desktop-parity-d22-branch-prefix.md, Task 3.
Review Focus 4: a clashing or reserved prefix is refused server-side, and validation
never runs a shell. A custom-prefixed branch is created, recorded and deleted only
through its record (Tasks 1-2).
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import delete, select, update

from viberpit import brand, gitrepos, worktrees
from server.db import db_session
from server.db.models import AppSettings, SessionBranch
from server.sessions import branch_ownership as bo
from server.sessions import branch_prefix as bp
from server.tests.test_session_fork import _fork, _git, env  # noqa: F401

IDENTITY = {"Tailscale-User-Login": "testuser@example.com"}


# ── format and reserved namespaces ─────────────────────────────────────────────

@pytest.mark.parametrize("prefix,why", [
    ("agent", "must end with /"),
    ("/", "2–40 characters"),
    ("a" * 40 + "/", "2–40 characters"),
    ("feature/", "common branch namespace"),
    ("Feat/", "common branch namespace"),
    ("dependabot/", "common branch namespace"),
    ("a..b/", "not a valid git branch prefix"),
    ("bad name/", "not a valid git branch prefix"),
    ("-x/", "not a valid git branch prefix"),
    ("x.lock/", "not a valid git branch prefix"),
])
def test_format_problems(prefix, why):
    problem = bp.format_problem(prefix)
    assert problem is not None and why in problem


@pytest.mark.parametrize("prefix", ["agent/", "vp/", "team/bots/", "a" * 39 + "/"])
def test_well_formed_prefixes_pass(prefix):
    assert bp.format_problem(prefix) is None


def test_format_check_runs_git_with_an_argv_list_never_a_shell():
    calls = []
    real = subprocess.run

    def spy(args, *a, **k):
        calls.append((args, k.get("shell", False)))
        return real(args, *a, **k)
    with patch.object(bp.subprocess, "run", spy):
        assert bp.format_problem("agent; rm -rf ~/") is not None
    assert calls == [(["git", "check-ref-format", "--branch", "agent; rm -rf ~/x"], False)]


# ── clashes with unrecorded local branches ─────────────────────────────────────

def _clone(root: Path, repo: str, *branches: str) -> Path:
    clone = gitrepos.clone_path(str(root), repo)
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@e",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    for b in branches:
        subprocess.run(["git", "-C", str(clone), "branch", b], check=True)
    return clone


def test_an_unrecorded_branch_under_the_prefix_is_a_clash(tmp_path):
    _clone(tmp_path, "acme/one", "agent/mine")
    with pytest.raises(bp.BranchPrefixInvalid) as ei:
        bp.validate("agent/", str(tmp_path), {})
    assert "one:agent/mine" in ei.value.reason


def test_a_recorded_branch_under_the_prefix_is_not(tmp_path):
    clone = _clone(tmp_path, "acme/one", "agent/made-by-us")
    bp.validate("agent/", str(tmp_path), {bo.clone_key(str(clone)) or "": frozenset(
        {"agent/made-by-us"})})


def test_a_branch_named_like_the_prefix_is_a_clash(tmp_path):
    _clone(tmp_path, "acme/one", "agent")          # git cannot also have agent/…
    with pytest.raises(bp.BranchPrefixInvalid):
        bp.validate("agent/", str(tmp_path), {})


def test_a_refusal_names_at_most_three_branches(tmp_path):
    _clone(tmp_path, "acme/one", *(f"agent/b{i}" for i in range(5)))
    with pytest.raises(bp.BranchPrefixInvalid) as ei:
        bp.validate("agent/", str(tmp_path), {})
    reason = ei.value.reason
    assert reason.count("one:agent/b") == 3 and "and 2 more" in reason


def test_a_checkout_directly_in_the_repo_root_is_checked_too(tmp_path):
    plain = tmp_path / "mycheckout"
    subprocess.run(["git", "init", "-q", "-b", "main", str(plain)], check=True)
    subprocess.run(["git", "-C", str(plain), "-c", "user.name=t", "-c", "user.email=t@e",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(plain), "branch", "agent/x"], check=True)
    with pytest.raises(bp.BranchPrefixInvalid):
        bp.validate("agent/", str(tmp_path), {})


def test_a_built_in_prefix_is_always_accepted(tmp_path):
    _clone(tmp_path, "acme/one", f"{brand.BRANCH_PREFIX}someone-else")
    bp.validate(brand.BRANCH_PREFIX, str(tmp_path), {})


# ── round 2: parent folders, ref namespaces, fail closed and bounded ──────────

@pytest.mark.parametrize("prefix", ["refs/", "heads/", "Tags/", "remotes/", "HEAD/",
                                    "origin/", "upstream/", "heads/bots/"])
def test_ambiguous_ref_namespaces_are_refused(prefix):
    problem = bp.format_problem(prefix)
    assert problem is not None and "ref namespaces" in problem


def test_the_ambiguous_list_is_separate_from_the_team_reserved_list():
    assert not bp.AMBIGUOUS_REF_NAMESPACES & bp.RESERVED_PREFIXES


@pytest.mark.parametrize("existing", ["team", "team/agent"])
def test_a_branch_named_like_any_parent_folder_is_a_clash_even_if_recorded(
        tmp_path, existing):
    """git refuses team/agent/x next to a branch `team` (or `team/agent`), recorded or
    not — so a record never exempts a parent-folder branch."""
    clone = _clone(tmp_path, "acme/one", existing)
    recorded = {bo.clone_key(str(clone)) or "": frozenset({existing})}
    with pytest.raises(bp.BranchPrefixInvalid) as ei:
        bp.validate("team/agent/", str(tmp_path), recorded)
    assert f"one:{existing}" in ei.value.reason


def test_a_clone_that_cannot_be_listed_refuses(tmp_path, monkeypatch):
    _clone(tmp_path, "acme/one")
    real = subprocess.run

    def timing_out(args, *a, **k):
        if "for-each-ref" in args:
            raise subprocess.TimeoutExpired(args, k.get("timeout"))
        return real(args, *a, **k)
    monkeypatch.setattr(bp.subprocess, "run", timing_out)
    with pytest.raises(bp.BranchPrefixInvalid) as ei:
        bp.clashing_branches("agent/", str(tmp_path), {})
    assert "Couldn't check existing branches in one" in ei.value.reason


def test_a_failing_listing_refuses(tmp_path, monkeypatch):
    _clone(tmp_path, "acme/one")
    real = subprocess.run

    def failing(args, *a, **k):
        if "for-each-ref" in args:
            return subprocess.CompletedProcess(args, 128, "", "fatal: bad repo")
        return real(args, *a, **k)
    monkeypatch.setattr(bp.subprocess, "run", failing)
    with pytest.raises(bp.BranchPrefixInvalid):
        bp.clashing_branches("agent/", str(tmp_path), {})


def test_the_whole_scan_has_a_deadline(tmp_path):
    _clone(tmp_path, "acme/one")
    with pytest.raises(bp.BranchPrefixInvalid) as ei:
        bp.clashing_branches("agent/", str(tmp_path), {}, deadline_s=0)
    assert "took too long" in ei.value.reason


def test_an_unreadable_repo_root_refuses_instead_of_raising(tmp_path):
    clones = _clone(tmp_path, "acme/one").parent.parent      # <clones base>
    os.chmod(clones, 0)
    try:
        with pytest.raises(bp.BranchPrefixInvalid) as ei:
            bp.validate("agent/", str(tmp_path), {})
    finally:
        os.chmod(clones, 0o755)
    assert "Couldn't check existing branches" in ei.value.reason


def test_clone_top_is_bounded_and_a_timeout_is_no_clone(tmp_path, monkeypatch):
    seen = {}

    def hung(args, *a, **k):
        seen["timeout"] = k.get("timeout")
        raise subprocess.TimeoutExpired(args, k.get("timeout"))
    monkeypatch.setattr(bo.subprocess, "run", hung)
    assert bo.clone_top(str(tmp_path)) is None
    assert seen["timeout"] == bo.CLONE_TOP_TIMEOUT_S


def test_n_may_repeat_across_two_custom_prefixes_and_that_is_harmless(tmp_path):
    """The fork-number guarantee is exactly the built-ins plus the current prefix."""
    clone = _clone(tmp_path, "acme/one")
    sid = "abcd1234-0000-0000-0000-000000000000"
    _git(clone, "branch", worktrees.fork_branch_name(sid, 1, "old/"))
    assert worktrees._next_fork_branch(str(clone), sid, "new/") == "new/fork-abcd1234-1"
    _git(clone, "branch", worktrees.fork_branch_name(sid, 1))
    assert worktrees._next_fork_branch(str(clone), sid, "new/") == "new/fork-abcd1234-2"


# ── the settings API ───────────────────────────────────────────────────────────

@contextmanager
def _client(repo_root: Path):
    from fastapi.testclient import TestClient
    from server.app import create_app
    from server.sessions.orchestrator import SessionOrchestrator
    from tests.helpers import temp_env

    with temp_env(), tempfile.TemporaryDirectory() as d:
        db_url = f"sqlite+aiosqlite:///{Path(d) / 'settings.db'}"
        with patch.dict(os.environ, {"VIBERPIT_DB_URL": db_url,
                                     "VIBERPIT_REPO_ROOT": str(repo_root)}):
            orch = SessionOrchestrator(poll_interval=9999.0)
            client = TestClient(create_app(orchestrator=orch), raise_server_exceptions=False)
            with client:
                yield client


def test_put_get_and_reset_round_trip(tmp_path):
    _clone(tmp_path, "acme/one", "feature")
    with _client(tmp_path) as client:
        def put(body):
            return client.put("/api/settings", json=body, headers=IDENTITY)
        got = client.get("/api/settings", headers=IDENTITY).json()
        assert got["branchPrefix"] is None
        assert got["effectiveDefaults"]["branchPrefix"] == brand.BRANCH_PREFIX
        r = put({"branchPrefix": " agent/ "})
        assert r.status_code == 200
        assert r.json()["branchPrefix"] == "agent/"
        # effectiveDefaults is what a NULL setting falls back to — never the stored value.
        assert r.json()["effectiveDefaults"]["branchPrefix"] == brand.BRANCH_PREFIX
        assert put({"thinkingCollapsed": False}).json()["branchPrefix"] == "agent/"
        r = put({"branchPrefix": None})                        # Reset
        assert r.json()["branchPrefix"] is None
        assert r.json()["effectiveDefaults"]["branchPrefix"] == brand.BRANCH_PREFIX
        assert put({"branchPrefix": "agent/"}).status_code == 200
        assert put({"branchPrefix": "  "}).json()["branchPrefix"] is None


@pytest.mark.parametrize("prefix", ["feature/", "agent", "a..b/"])
def test_an_invalid_prefix_is_a_400_with_a_reason_and_changes_nothing(tmp_path, prefix):
    with _client(tmp_path) as client:
        r = client.put("/api/settings", json={"branchPrefix": prefix}, headers=IDENTITY)
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert detail["code"] == "BRANCH_PREFIX_INVALID" and detail["reason"]
        assert client.get("/api/settings", headers=IDENTITY).json()["branchPrefix"] is None


def test_an_unreadable_root_is_a_400_not_a_500(tmp_path):
    clones = _clone(tmp_path, "acme/one").parent.parent
    with _client(tmp_path) as client:
        os.chmod(clones, 0)
        try:
            r = client.put("/api/settings", json={"branchPrefix": "agent/"}, headers=IDENTITY)
        finally:
            os.chmod(clones, 0o755)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "BRANCH_PREFIX_INVALID"


def test_re_saving_an_unchanged_prefix_skips_the_clash_scan(tmp_path):
    """An unrelated save that re-sends the stored prefix (an older client does) must not
    fail on a branch that appeared under it since — nothing about the prefix changed."""
    clone = _clone(tmp_path, "acme/one")
    with _client(tmp_path) as client:
        assert client.put("/api/settings", json={"branchPrefix": "agent/"},
                          headers=IDENTITY).status_code == 200
        _git(clone, "branch", "agent/not-ours")                  # unrecorded clash now
        r = client.put("/api/settings", json={"branchPrefix": "agent/",
                                              "thinkingCollapsed": False}, headers=IDENTITY)
        assert r.status_code == 200
        assert r.json()["thinkingCollapsed"] is False and r.json()["branchPrefix"] == "agent/"
        # A CHANGED prefix is still validated.
        r = client.put("/api/settings", json={"branchPrefix": "agent/not-ours/"},
                       headers=IDENTITY)
        assert r.status_code == 400


def test_a_clashing_prefix_is_a_400_naming_the_branch(tmp_path):
    _clone(tmp_path, "acme/one", "agent/mine")
    with _client(tmp_path) as client:
        r = client.put("/api/settings", json={"branchPrefix": "agent/"}, headers=IDENTITY)
        assert r.status_code == 400
        assert "one:agent/mine" in r.json()["detail"]["reason"]


# ── custom-prefix spawn and fork: created, recorded, deleted via the record ────

async def _set_prefix(prefix: str | None) -> None:
    async with db_session() as db:
        row = await db.get(AppSettings, "singleton")
        if row is None:
            db.add(AppSettings(id="singleton", branch_prefix=prefix))
        else:
            await db.execute(update(AppSettings).where(AppSettings.id == "singleton")
                             .values(branch_prefix=prefix))


async def _records() -> set[tuple[str, str, str]]:
    async with db_session() as db:
        return {(r.session_id, r.branch, r.source)
                for r in (await db.execute(select(SessionBranch))).scalars()}


@pytest.mark.asyncio
async def test_a_custom_prefix_spawn_is_created_recorded_and_deleted_on_close(  # noqa: F811
        env) -> None:  # noqa: F811 — pytest fixture
    orch, _spawns, _src, _root, clone = env
    await _set_prefix("agent/")
    rec = await orch.create_session("Child", cwd=str(clone), branch="main")
    assert rec.branch == worktrees._child_branch("main", rec.id, "agent/")
    assert rec.branch.startswith("agent/main-")
    assert (rec.id, rec.branch, bo.SOURCE_CREATE) in await _records()
    await orch.close_session(rec.id)
    assert not worktrees.local_branch_exists(str(clone), rec.branch)
    assert all(r[0] != rec.id for r in await _records())


@pytest.mark.asyncio
async def test_a_custom_prefix_fork_is_created_recorded_and_deleted_on_close(  # noqa: F811
        env) -> None:  # noqa: F811 — pytest fixture
    orch, _spawns, src, _root, clone = env
    # An older fork under the built-in prefix holds n=1: the new prefix must not reuse it.
    _git(clone, "branch", worktrees.fork_branch_name(src.id, 1))
    await _set_prefix("agent/")
    out = await _fork(orch, src.id, at_turn_id="t1", mode="reply")
    child = orch.record(out["sessionId"])
    assert child.branch == worktrees.fork_branch_name(src.id, 2, "agent/")
    assert (child.id, child.branch, bo.SOURCE_FORK) in await _records()
    # The fork carries the source's uncommitted work; close never removes a dirty tree.
    _git(child.cwd, "reset", "-q", "--hard")
    _git(child.cwd, "clean", "-qfd")
    await orch.close_session(child.id)
    assert not worktrees.local_branch_exists(str(clone), child.branch)
    assert all(r[0] != child.id for r in await _records())


@pytest.mark.asyncio
async def test_an_unrecorded_custom_prefix_branch_is_kept_on_close(env) -> None:  # noqa: F811
    """The prefix never makes a name deletable: without its record, a custom-prefixed
    branch survives its session's close."""
    orch, _spawns, _src, _root, clone = env
    await _set_prefix("agent/")
    rec = await orch.create_session("Child", cwd=str(clone), branch="main")
    async with db_session() as db:
        await db.execute(delete(SessionBranch))
    await orch.close_session(rec.id)
    assert worktrees.local_branch_exists(str(clone), rec.branch)


@pytest.mark.asyncio
async def test_the_prefix_resolver_falls_back_to_the_built_in_when_unreadable(  # noqa: F811
        env, monkeypatch) -> None:  # noqa: F811 — pytest fixture
    orch, _spawns, _src, _root, clone = env

    def broken():
        raise RuntimeError("db down")
    monkeypatch.setattr("server.db.db_session", broken)
    assert await bp.effective_branch_prefix() == brand.BRANCH_PREFIX
