"""One capability edit on a product's map-changes branch (roadmap drill-down
spec §8/§9).

The service is the only thing that writes ``pm/map-changes-<slug>``: it loads
or opens the pointer row (``feature_map_changes``), reads the doc at the branch
head, applies a pure op from :mod:`server.pm.map_changes_ops`, commits it
atomically through :mod:`server.pm.map_changes_gh`, opens the PR on the first
edit and asks pm-sync for a pass so the map shows the proposal (R8b-1: no agent
session runs for a deterministic doc edit).

Every GitHub call is the injectable ``GhWrite`` seam; the DB session belongs to
the caller (the API handler commits), so an edit and its audit row land in one
unit of work.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from ccswitch import gitrepos
from server.audit import record_audit
from server.db.models import Feature, FeatureMapChange, Project, ProjectRepo

from .classification import FEATURE_DOCS_DIR, feature_doc_path
from .featuredoc import FeatureDoc, FeatureDocError, parse_feature_doc
from .map_changes_gh import GhWrite, GhWriteError
from .map_changes_ops import (
    MapContext,
    MapOp,
    MapOpError,
    apply_op,
    commit_message,
    validate_op,
)
from .sync_request import request_project_sync

log = logging.getLogger(__name__)

MAP_CHANGES_STATUS_OPEN = "open"


def map_changes_branch_name(project_slug: str) -> str:
    """The one ccswitch-owned map-changes branch for a product. Deterministic,
    like ``classification.map_branch_name`` (the archaeology map PR) — but a
    distinct branch: that one is a one-shot synthesis, this one is long-lived."""
    return f"pm/map-changes-{project_slug}"


async def resolve_default_branch(repo: str) -> str:
    """A repo's default branch (``main``/``master``) — never hardcoded.

    THE definition, shared by ``SessionOrchestrator._sdlc_default_branch`` and
    the map-changes service's own default: the branch a map-changes PR targets
    and the SHA it branches from must agree with every other base-branch
    resolution in the server. Degrades to ``master`` rather than raising —
    callers are request handlers and polling loops."""
    try:
        return await asyncio.to_thread(
            lambda: gitrepos.list_remote_branches(repo).get("default") or "master")
    except Exception:
        return "master"


PR_BODY = """\
Capability-map changes for this product, opened and maintained by ccswitch.

Every inline capability edit in the roadmap UI appends one commit to this
branch; the map shows those capabilities as **proposed** until this PR merges.
Closing it (the roadmap's *Abandon* action) drops every change on it.

The first machine edit of a document rewrites its frontmatter into the
canonical `slug` / `title` / `state` / `parent` / `links` form. The prose body
is left alone.

If the commits here are authored by a personal token rather than the ccswitch
GitHub App, granting the App `contents:write` and `pull_requests:write` on this
repository makes them bot-authored.
"""


class NoPrimaryRepo(RuntimeError):
    """The product has no primary repo, so there is nowhere to write docs."""


class NoOpenMapChanges(RuntimeError):
    """No open map-changes pointer for this product."""


class NotBehind(RuntimeError):
    """Rebase was asked for a PR GitHub does not report as behind its base."""


def _aware(dt: datetime | None) -> datetime | None:
    """Normalize a ``DateTime(timezone=True)`` value to tz-aware UTC — SQLite
    hands these back naive. Mirrors ``server/api/pm.py``'s module-private
    ``_aware`` (that one is deliberately local to its module)."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Pointer:
    """The wire shape of a pointer row: where the branch/PR is and what GitHub
    last said about it. Also what the map payload carries per product."""

    branch: str
    pr_number: int | None
    pr_url: str | None
    status: str
    opened_at: str | None
    head_sha: str | None
    behind: bool
    conflicting: bool

    @classmethod
    def from_row(cls, row: FeatureMapChange) -> Pointer:
        opened = _aware(row.opened_at)
        return cls(
            branch=row.branch or "",
            pr_number=row.pr_number,
            pr_url=row.pr_url,
            status=row.status or MAP_CHANGES_STATUS_OPEN,
            opened_at=opened.isoformat() if opened else None,
            head_sha=row.head_sha,
            behind=bool(row.behind),
            conflicting=bool(row.conflicting),
        )

    def to_dict(self) -> dict:
        return {
            "branch": self.branch,
            "prNumber": self.pr_number,
            "prUrl": self.pr_url,
            "status": self.status,
            "openedAt": self.opened_at,
            "headSha": self.head_sha,
            "behind": self.behind,
            "conflicting": self.conflicting,
        }


class MapChangesService:
    """Edits on a product's map-changes branch. ``gh_factory`` builds the write
    seam for a repo (so the orchestrator can inject its ``gh`` runner) and
    ``default_branch`` resolves the repo's base branch — never hardcode it."""

    def __init__(
        self,
        gh_factory: Callable[[str], GhWrite] = GhWrite,
        default_branch: Callable[[str], Awaitable[str]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._gh_factory = gh_factory
        self._default_branch = default_branch or resolve_default_branch
        self._now = now or (lambda: datetime.now(timezone.utc))

    # -- reads ----------------------------------------------------------------

    async def open_pointer(self, db, project: Project) -> FeatureMapChange | None:
        """The product's open pointer row, if any. This lookup is what enforces
        "at most one open map-changes PR per product" (R8b-5)."""
        return (await db.execute(select(FeatureMapChange).where(
            FeatureMapChange.project_id == project.id,
            FeatureMapChange.status == MAP_CHANGES_STATUS_OPEN,
        ).order_by(FeatureMapChange.opened_at.desc()))).scalars().first()

    async def _primary_repo(self, db, project: Project) -> str:
        row = (await db.execute(select(ProjectRepo).where(
            ProjectRepo.project_id == project.id,
            ProjectRepo.is_primary.is_(True)))).scalars().first()
        if row is None or not row.repo:
            raise NoPrimaryRepo(
                f"product '{project.slug}' has no primary repo to write capability docs to")
        return row.repo

    def _branch_docs(self, gh: GhWrite, branch: str) -> list[FeatureDoc]:
        """Every parseable feature doc on the branch. Unparseable ones are
        skipped: a human commit on the branch must not break validation."""
        docs: list[FeatureDoc] = []
        for path in gh.list_dir(FEATURE_DOCS_DIR, branch):
            text, _blob = gh.read_file(path, branch)
            if not text:
                continue
            try:
                docs.append(parse_feature_doc(text))
            except FeatureDocError:
                log.info("map_changes: skipping unparseable doc %s on %s", path, branch)
        return docs

    async def context(
        self, db, project: Project, gh: GhWrite, pointer: FeatureMapChange | None,
    ) -> MapContext:
        """What validation needs: the product's capability slugs → parent slug
        (indexed rows, overridden by the branch's docs — the branch is truth for
        anything proposed there) and every link target in the index."""
        rows = (await db.execute(select(Feature).where(
            Feature.project_id == project.id,
            Feature.orphaned.isnot(True)))).scalars().all()
        slug_by_id = {r.id: r.slug for r in rows}
        parents: dict[str, str | None] = {
            r.slug: slug_by_id.get(r.parent_feature_id) for r in rows if r.slug}
        if pointer is not None and pointer.branch:
            for doc in await asyncio.to_thread(self._branch_docs, gh, pointer.branch):
                parents[doc.slug] = doc.parent
        targets = (await db.execute(
            select(Project.slug, Feature.slug).join(
                Feature, Feature.project_id == Project.id).where(
                    Feature.orphaned.isnot(True)))).all()
        return MapContext(
            parents=parents,
            link_targets=frozenset(
                (str(ps), str(fs)) for ps, fs in targets if ps and fs),
        )

    # -- writes ---------------------------------------------------------------

    async def edit(
        self, db, project: Project, op: MapOp, identity: str,
    ) -> tuple[Pointer, str, str]:
        """Apply one edit and return ``(pointer, doc path, commit sha)``."""
        repo = await self._primary_repo(db, project)
        branch = map_changes_branch_name(project.slug or "")
        gh = self._gh_factory(repo)
        pointer = await self.open_pointer(db, project)
        validate_op(op, await self.context(db, project, gh, pointer))

        base = await self._default_branch(repo)
        base_sha = await asyncio.to_thread(gh.default_branch_head, base)
        # Re-read the branch head even when the row has one: the row may be
        # stale (a merge, or a commit by another writer) and the read is cheap.
        head = await asyncio.to_thread(gh.ensure_branch, branch, base_sha)
        path = feature_doc_path(op.slug)
        current, _blob = await asyncio.to_thread(gh.read_file, path, branch)
        if op.op == "create" and current is not None:
            raise MapOpError("slug_taken", f"'{path}' already exists on {branch}")
        path, text = apply_op(op, current)
        message = commit_message(op, project.name or project.slug or "")
        try:
            sha = await asyncio.to_thread(gh.commit_file, branch, head, path, text, message)
        except GhWriteError as e:
            if not e.conflict:
                raise
            # Someone committed between the read and the write. One re-read and
            # retry; a second conflict is the caller's to resolve (409).
            head = await asyncio.to_thread(gh.ensure_branch, branch, base_sha)
            sha = await asyncio.to_thread(gh.commit_file, branch, head, path, text, message)

        now = self._now()
        if pointer is None:
            pr = await asyncio.to_thread(
                gh.open_pr, branch, base, f"Capability map changes: {project.name}", PR_BODY)
            pointer = FeatureMapChange(
                project_id=project.id, repo=repo, branch=branch,
                pr_number=pr.number or None, pr_url=pr.url or None,
                status=MAP_CHANGES_STATUS_OPEN, head_sha=sha,
                behind=pr.behind, conflicting=pr.conflicting, opened_at=now)
            db.add(pointer)
        else:
            pointer.head_sha = sha
        await db.flush()
        await request_project_sync(db, project.id, now)
        await record_audit(
            db=db, type=f"pm.map_changes.{op.op}", identity=identity,
            detail={"slug": project.slug, "op": op.op, "capability": op.slug,
                    "path": path, "commitSha": sha, "prNumber": pointer.pr_number})
        return Pointer.from_row(pointer), path, sha

    async def abandon(self, db, project: Project, identity: str) -> Pointer:
        """Close the PR, delete the branch and mark the row abandoned (R8b-7).
        Feature-row cleanup for never-merged proposals is the sync loop's."""
        pointer = await self.open_pointer(db, project)
        if pointer is None:
            raise NoOpenMapChanges(f"product '{project.slug}' has no open map-changes PR")
        repo = pointer.repo or await self._primary_repo(db, project)
        gh = self._gh_factory(repo)
        if pointer.pr_number:
            await asyncio.to_thread(gh.close_pr, pointer.pr_number)
        if pointer.branch:
            await asyncio.to_thread(gh.delete_branch, pointer.branch)
        pointer.status = "abandoned"
        pointer.closed_at = self._now()
        await db.flush()
        await record_audit(
            db=db, type="pm.map_changes.abandon", identity=identity,
            detail={"slug": project.slug, "branch": pointer.branch,
                    "prNumber": pointer.pr_number})
        return Pointer.from_row(pointer)

    async def rebase(self, db, project: Project, identity: str) -> Pointer:
        """Merge the base branch into the PR through GitHub's own update-branch
        (R8b-8) — offered only while GitHub reports the PR behind, and never a
        force-push."""
        pointer = await self.open_pointer(db, project)
        if pointer is None:
            raise NoOpenMapChanges(f"product '{project.slug}' has no open map-changes PR")
        if not pointer.behind or not pointer.pr_number:
            raise NotBehind(f"map-changes PR for '{project.slug}' is not behind its base")
        gh = self._gh_factory(pointer.repo or await self._primary_repo(db, project))
        await asyncio.to_thread(gh.update_branch, pointer.pr_number)
        # GitHub has merged the base in; the next PR read re-derives the truth.
        pointer.behind = False
        await db.flush()
        await record_audit(
            db=db, type="pm.map_changes.rebase", identity=identity,
            detail={"slug": project.slug, "branch": pointer.branch,
                    "prNumber": pointer.pr_number})
        return Pointer.from_row(pointer)
