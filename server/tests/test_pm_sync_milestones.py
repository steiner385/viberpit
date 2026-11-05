"""The pm-sync milestones pass: read docs/product/milestones.yaml off the primary
repo, plan against the DB rows, apply through the real seams (isolated_db)."""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from server.automations.pm_sync import PmSyncLoop, RepoSyncView
from server.db import db_session
from server.db.models import Milestone, MilestoneLadder, Project
from server.pm.feature_index import FeatureDocsRead
from server.pm.milestone_def import MILESTONES_PATH
from server.pm.milestone_sync_seams import apply_ladder_plan, load_ladder_rows

FIXTURE = (Path(__file__).parent / "fixtures" / "milestones" / "ladder.yaml").read_text()
REPO = "cairnea/KinDash"


class Ctx:
    def __init__(self):
        self.events = []

    async def emit(self, level, text, **kw):
        self.events.append((level, text, kw))


async def _project() -> str:
    async with db_session() as db:
        p = Project(slug="kd", name="kd")
        db.add(p)
        await db.flush()
        return p.id


def _view(project_id: str, *, is_primary: bool = True) -> RepoSyncView:
    return RepoSyncView(
        project_id=project_id, project_slug="kd", repo=REPO, is_primary=is_primary,
        backfill_done=True, backfill_phase="prs", backfill_cursor=None,
        watermark=None, last_synced_at=None, sync_requested_at=None)


def _loop(read_milestones) -> PmSyncLoop:
    async def _none(*_a, **_k):
        return None

    async def _docs(repo, ref=None):
        return FeatureDocsRead(sha="s", docs=[])

    async def _rows(_pid):
        return []

    return PmSyncLoop(
        Ctx(), read_config=_none, list_repos=_none, fetch=_none, upsert_items=_none,
        save_sync_state=_none, read_feature_docs=_docs, list_feature_rows=_rows,
        apply_feature_plan=_none, now_utc=lambda: datetime(2026, 11, 5, tzinfo=timezone.utc),
        read_milestones=read_milestones, load_ladder_rows=load_ladder_rows,
        apply_ladder_plan=apply_ladder_plan)


async def _state() -> tuple[MilestoneLadder | None, dict[str, Milestone]]:
    async with db_session() as db:
        ladder = (await db.execute(select(MilestoneLadder))).scalar_one_or_none()
        rows = (await db.execute(select(Milestone))).scalars().all()
        return ladder, {m.key: m for m in rows}


def _reader(text, sha="sha1"):
    async def read(repo):
        assert repo == REPO
        return text, sha
    return read


@pytest.mark.asyncio
async def test_first_pass_creates_ladder_with_start_current(isolated_db):
    pid = await _project()
    loop = _loop(_reader(FIXTURE))
    loop._observation_degraded = False
    await loop._milestones_pass(_view(pid))
    ladder, rows = await _state()
    assert ladder is not None and ladder.project_id == pid
    assert ladder.source_path == MILESTONES_PATH and ladder.source_sha == "sha1"
    assert ladder.synced_at is not None and ladder.parse_error is None
    assert ladder.title == "Launch readiness"
    assert ladder.definition["start"] == "household"
    assert len(rows) == 4
    assert rows["household"].state == "current" and rows["household"].entered_at is not None
    assert {rows[k].state for k in ("friendly", "community", "public")} == {"upcoming"}
    assert [rows[k].ordinal for k in ("household", "friendly", "community", "public")] == [0, 1, 2, 3]
    assert loop._observation_degraded is False


@pytest.mark.asyncio
async def test_second_pass_keeps_live_state_and_records_parse_error(isolated_db):
    pid = await _project()
    await _loop(_reader(FIXTURE))._milestones_pass(_view(pid))
    async with db_session() as db:
        row = (await db.execute(select(Milestone).where(Milestone.key == "friendly"))).scalar_one()
        row.status_note = "long phase"
    # a good re-sync never touches live state
    await _loop(_reader(FIXTURE, "sha2"))._milestones_pass(_view(pid))
    ladder, rows = await _state()
    assert rows["friendly"].status_note == "long phase"
    assert rows["household"].state == "current"
    assert ladder is not None and ladder.source_sha == "sha2"
    first_synced = ladder.synced_at
    # a parse error keeps the last good definition and its commit
    await _loop(_reader("version: 1\nbogus: x\n", "sha3"))._milestones_pass(_view(pid))
    ladder, rows = await _state()
    assert ladder is not None
    assert ladder.parse_error and "bogus" in ladder.parse_error
    assert ladder.parse_error_sha == "sha3"
    assert ladder.source_sha == "sha2" and ladder.definition["title"] == "Launch readiness"
    assert ladder.synced_at >= first_synced
    assert len(rows) == 4
    # the file disappears: rows kept, error recorded
    await _loop(_reader(None, "sha4"))._milestones_pass(_view(pid))
    ladder, rows = await _state()
    assert ladder is not None and ladder.parse_error == "definition file removed"
    assert len(rows) == 4 and all(r.removed_at is None for r in rows.values())


@pytest.mark.asyncio
async def test_read_failure_degrades_and_leaves_rows_untouched(isolated_db):
    pid = await _project()
    await _loop(_reader(FIXTURE))._milestones_pass(_view(pid))
    before_ladder, before = await _state()

    async def boom(repo):
        raise RuntimeError("clone failed")

    loop = _loop(boom)
    loop._observation_degraded = False
    await loop._milestones_pass(_view(pid))
    assert loop._observation_degraded is True
    after_ladder, after = await _state()
    assert after_ladder is not None and before_ladder is not None
    assert after_ladder.synced_at == before_ladder.synced_at
    assert after_ladder.source_sha == before_ladder.source_sha
    assert {k: (r.state, r.ordinal, r.removed_at) for k, r in after.items()} == \
        {k: (r.state, r.ordinal, r.removed_at) for k, r in before.items()}


@pytest.mark.asyncio
async def test_non_primary_repo_is_skipped(isolated_db):
    pid = await _project()

    async def must_not_read(repo):
        raise AssertionError("must not be called for a non-primary repo")

    loop = _loop(must_not_read)
    loop._observation_degraded = False
    await loop._milestones_pass(_view(pid, is_primary=False))
    assert loop._observation_degraded is False
    ladder, rows = await _state()
    assert ladder is None and rows == {}


@pytest.mark.asyncio
async def test_pass_runs_in_incremental_after_governance(isolated_db):
    """Wired at the incremental call site: one empty-delta pass syncs the ladder."""
    pid = await _project()
    pages = [
        {"data": {"repository": {"issues": {"pageInfo": {"endCursor": None, "hasNextPage": False},
                                             "nodes": []}}}},
        {"data": {"repository": {"pullRequests": {"pageInfo": {"endCursor": None, "hasNextPage": False},
                                                   "nodes": []}}}},
    ]
    loop = _loop(_reader(FIXTURE))

    async def fetch(_q):
        return pages.pop(0)

    loop._fetch = fetch
    loop._observation_degraded = False
    await loop._incremental(_view(pid), datetime(2026, 11, 5, tzinfo=timezone.utc))
    _, rows = await _state()
    assert len(rows) == 4 and rows["household"].state == "current"


# ---------------------------------------------------------------- orchestrator read seam


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo(root: Path, text: str | None) -> tuple[str, str]:
    clone = root / "clone"
    clone.mkdir()
    _git("init", "-q", str(clone), cwd=root)
    _git("config", "user.email", "t@t", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    (clone / "README.md").write_text("x\n")
    if text is not None:
        (clone / "docs" / "product").mkdir(parents=True)
        (clone / MILESTONES_PATH).write_text(text)
    _git("add", "-A", cwd=clone)
    _git("commit", "-q", "-m", "init", cwd=clone)
    head = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    _git("update-ref", "refs/remotes/origin/master", head, cwd=clone)
    return str(clone), head


def _orch(clone: str):
    from server.sessions import orchestrator as mod

    orch = mod.SessionOrchestrator.__new__(mod.SessionOrchestrator)

    async def root():
        return "/tmp/root"

    orch._effective_repo_root = root  # type: ignore[method-assign]
    return orch, patch.object(mod.gitrepos, "ensure_clone", return_value=clone)


@pytest.mark.asyncio
async def test_read_milestones_returns_text_and_head_sha(tmp_path):
    clone, head = _repo(tmp_path, FIXTURE)
    orch, p = _orch(clone)
    with p:
        text, sha = await orch._pm_sync_read_milestones("o/r")
    assert text == FIXTURE and sha == head


@pytest.mark.asyncio
async def test_read_milestones_missing_file_returns_none_with_sha(tmp_path):
    clone, head = _repo(tmp_path, None)
    orch, p = _orch(clone)
    with p:
        text, sha = await orch._pm_sync_read_milestones("o/r")
    assert text is None and sha == head


@pytest.mark.asyncio
async def test_read_milestones_raises_when_ref_unresolvable(tmp_path):
    clone = tmp_path / "empty"
    clone.mkdir()
    _git("init", "-q", str(clone), cwd=tmp_path)
    orch, p = _orch(str(clone))
    with p, pytest.raises(RuntimeError):
        await orch._pm_sync_read_milestones("o/r")
