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
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, or_, select, update

from ccswitch import brand
from ccswitch import gitrepos
from server.audit import record_audit
from server.db.models import (
    FEATURE_MAP_CHANGE_STATUSES,
    Feature,
    FeatureLink,
    FeatureMapChange,
    InitiativeFeature,
    Project,
    ProjectRepo,
    TriageProposal,
    WorkItem,
)

from .classification import FEATURE_DOCS_DIR, feature_doc_path
from .featuredoc import FeatureDoc, FeatureDocError, parse_feature_doc
from .map_changes_gh import GhWrite, GhWriteError, PrState
from .map_changes_ops import (
    SLUG_RE,
    MapContext,
    MapOp,
    MapOpError,
    apply_op,
    commit_message,
    validate_op,
    validate_op_shape,
)
from .sync_request import request_project_sync

log = logging.getLogger(__name__)

# The model's tuple is the one definition of a pointer's lifecycle; never
# re-spell these as literals here.
(MAP_CHANGES_STATUS_OPEN, MAP_CHANGES_STATUS_MERGED,
 MAP_CHANGES_STATUS_ABANDONED) = FEATURE_MAP_CHANGE_STATUSES


def map_changes_branch_name(project_slug: str) -> str:
    """The one ViberPit-owned map-changes branch for a product. Deterministic,
    like ``classification.map_branch_name`` (the archaeology map PR) — but a
    distinct branch: that one is a one-shot synthesis, this one is long-lived.

    The slug goes into REST paths and a git ref unencoded, so it must satisfy
    the same ``SLUG_RE`` a capability slug does. Project slugs are not
    validated at creation time yet (a follow-up on ``POST /api/pm/projects``),
    so this is where a non-branch-safe one is refused."""
    if not SLUG_RE.match(project_slug or ""):
        raise MapOpError("bad_slug", "project slug is not branch-safe")
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


PR_BODY = f"""\
Capability-map changes for this product, opened and maintained by {brand.NAME}.

Every inline capability edit in the roadmap UI appends one commit to this
branch; the map shows those capabilities as **proposed** until this PR merges.
Closing it (the roadmap's *Abandon* action) drops every change on it.

The first machine edit of a document rewrites its frontmatter into the
canonical `slug` / `title` / `state` / `parent` / `links` form. The prose body
is left alone.

If the commits here are authored by a personal token rather than the {brand.NAME}
GitHub App, granting the App `contents:write` and `pull_requests:write` on this
repository makes them bot-authored.
"""


async def open_pointer_rows(
    db, project_ids: Sequence[str],
) -> dict[str, FeatureMapChange]:
    """THE query for "a product's open map-changes pointer", batched by project.

    It is what enforces "at most one open map-changes PR per product" (R8b-5):
    there is no partial unique index (SQLite's reconcile path cannot add one),
    so every reader must agree on which row wins. Rows come back oldest-first
    and the dict keeps the last write, i.e. the NEWEST open row per project.

    Batched because the map payload needs one pointer per product in a single
    round trip (``server/api/pm_tree.py`` renders every product per request);
    :func:`open_pointer_row` is the one-project spelling over the same query.
    """
    ids = [pid for pid in project_ids if pid]
    if not ids:
        return {}
    rows = (await db.execute(select(FeatureMapChange).where(
        FeatureMapChange.project_id.in_(ids),
        FeatureMapChange.status == MAP_CHANGES_STATUS_OPEN,
    ).order_by(FeatureMapChange.opened_at))).scalars().all()
    return {row.project_id: row for row in rows}


async def open_pointer_row(db, project_id: str) -> FeatureMapChange | None:
    """The product's open pointer row, if any — see :func:`open_pointer_rows`."""
    return (await open_pointer_rows(db, [project_id])).get(project_id)


class NoPrimaryRepo(RuntimeError):
    """The product has no primary repo, so there is nowhere to write docs."""


class NoOpenMapChanges(RuntimeError):
    """No open map-changes pointer for this product."""


class NotBehind(RuntimeError):
    """Rebase was asked for a PR GitHub does not report as behind its base."""


def _slug_taken_on_branch(slug: str, branch: str) -> str:
    """The one wording for "that capability is already proposed on the branch".

    It names the branch on purpose: the doc can be there with no pointer row
    (a stranded commit), and then ``create`` is the ONE op that still refuses —
    every other op on the slug works and re-adopts the PR."""
    return (f"'{feature_doc_path(slug)}' already exists on {branch}; "
            f"edit that capability instead of creating it again")


def _aware(dt: datetime | None) -> datetime | None:
    """Normalize a ``DateTime(timezone=True)`` value to tz-aware UTC — SQLite
    hands these back naive. Mirrors ``server/api/pm.py``'s module-private
    ``_aware`` (that one is deliberately local to its module)."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


def _parse_gh_time(value: str | None) -> datetime | None:
    """GitHub's ``2026-09-11T10:00:00Z`` as an aware UTC datetime, or ``None``.
    Never raises: an unparseable timestamp falls back to the caller's clock
    rather than failing a polling pass."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.info("map_changes: unparseable GitHub timestamp %r", value)
        return None


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

    async def reconcile_updates(self) -> None:
        """Observe retained manual effects even after the map pointer is terminal.

        This is read-only at GitHub and independent of new-work policy. The
        stored repository selects the same owner credentials as the submission;
        neither credentials nor an enabled PR Doctor are required in the row.
        """
        from server.automations.mechanical_update import observe, release_manual
        from server.automations.ownership import StaleOwnership
        from server.db import db_session
        from server.db.models import AutomationOperation

        async with db_session() as db:
            operations = (await db.scalars(select(AutomationOperation).where(
                AutomationOperation.owner == "map-changes",
                AutomationOperation.phase.in_(("active", "uncertain", "completed"))))).all()
            pending = [(op.token, dict(op.provenance)) for op in operations]
        for token, provenance in pending:
            repo = provenance.get("repo")
            if not isinstance(repo, str) or provenance.get("mechanical_kind") != "update_branch":
                continue
            try:
                gh = await asyncio.to_thread(self._gh_factory, repo)

                async def transport(_repo, args):
                    return await asyncio.to_thread(gh.recovery_request, args)

                if not await observe(token, transport=transport):
                    continue
                await release_manual(token)
            except StaleOwnership:
                continue  # Another observer or the request tail already settled it.
            except Exception:
                log.warning("map_changes.reconcile_failed token=%s", token, exc_info=True)

    async def open_pointer(self, db, project: Project) -> FeatureMapChange | None:
        """The product's open pointer row, if any — :func:`open_pointer_row` is
        the one definition of that lookup, shared with the sync-loop seam and
        the map payload."""
        return await open_pointer_row(db, project.id)

    async def _primary_repo(self, db, project: Project) -> str:
        row = (await db.execute(select(ProjectRepo).where(
            ProjectRepo.project_id == project.id,
            ProjectRepo.is_primary.is_(True)))).scalars().first()
        if row is None or not row.repo:
            raise NoPrimaryRepo(
                f"product '{project.slug}' has no primary repo to write capability docs to")
        return row.repo

    def _read_branch_docs(self, gh: GhWrite, branch: str) -> list[FeatureDoc]:
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

    async def branch_docs(
        self, gh: GhWrite, branch: str, *, pointer: FeatureMapChange | None,
    ) -> list[FeatureDoc]:
        """The branch's docs, gated on the BRANCH existing — not on the pointer
        row existing.

        The two can disagree: the GitHub commit lands before the DB row, so a
        failure in between strands real docs on a branch no row points at. Were
        validation gated on the row, those docs would be invisible — every op on
        them would answer ``not_found`` while ``create`` answered ``slug_taken``,
        with no way out. An open pointer implies the branch, so the existence
        probe is only paid when there is no row."""
        if pointer is None:
            if not await asyncio.to_thread(gh.branch_exists, branch):
                return []
        elif not pointer.branch:
            return []
        return await asyncio.to_thread(self._read_branch_docs, gh, branch)

    async def context(
        self, db, project: Project, branch_docs: Sequence[FeatureDoc] = (),
    ) -> MapContext:
        """What validation needs: the product's capability slugs → parent slug
        (indexed rows, overridden by the branch's docs — the branch is truth for
        anything proposed there) and every link target.

        Link targets are the indexed ``(project slug, capability slug)`` pairs
        PLUS this product's branch docs (R8b-10 as amended): a capability
        proposed on the branch is as real a target as an indexed one for links
        made in the same PR — it just has not been indexed yet."""
        rows = (await db.execute(select(Feature).where(
            Feature.project_id == project.id,
            Feature.orphaned.isnot(True)))).scalars().all()
        slug_by_id = {r.id: r.slug for r in rows}
        parents: dict[str, str | None] = {
            r.slug: slug_by_id.get(r.parent_feature_id) for r in rows if r.slug}
        for doc in branch_docs:
            parents[doc.slug] = doc.parent
        targets = (await db.execute(
            select(Project.slug, Feature.slug).join(
                Feature, Feature.project_id == Project.id).where(
                    Feature.orphaned.isnot(True)))).all()
        link_targets = {(str(ps), str(fs)) for ps, fs in targets if ps and fs}
        link_targets |= {(str(project.slug), doc.slug) for doc in branch_docs if doc.slug}
        return MapContext(parents=parents, link_targets=frozenset(link_targets))

    # -- writes ---------------------------------------------------------------

    async def _adopt_or_open_pr(
        self, gh: GhWrite, branch: str, base: str, project: Project,
    ) -> PrState:
        """The product's map-changes PR: the open one if GitHub already has it,
        else a newly opened one.

        Adopt before opening: the commit and the PR land before the pointer row
        does, so a previous attempt may already have opened the PR (F2), and
        asking GitHub for a second one answers "A pull request already exists"
        forever.

        An adopted PR is then READ (``GET pulls/{n}``): the list endpoint
        ``find_open_pr`` uses carries no ``mergeable_state``, so building the row
        from it would record ``behind``/``conflicting`` as false for a PR that is
        in fact behind or in conflict — and the UI would offer neither Rebase nor
        the conflict banner until the next sync pass happened to correct it."""
        found = await asyncio.to_thread(gh.find_open_pr, branch, base)
        if found is None:
            title = f"Capability map changes: {project.name or project.slug}"
            return await asyncio.to_thread(gh.open_pr, branch, base, title, PR_BODY)
        log.info("map_changes: adopting existing PR #%s for %s", found.number, branch)
        if not found.number:
            return found
        return await asyncio.to_thread(gh.read_pr, found.number)

    async def edit(
        self, db, project: Project, op: MapOp, identity: str,
    ) -> tuple[Pointer, str, str]:
        """Apply one edit and return ``(pointer, doc path, commit sha)``."""
        # Cheap, context-free checks first: everything below costs GitHub reads.
        validate_op_shape(op)
        branch = map_changes_branch_name(project.slug or "")
        repo = await self._primary_repo(db, project)
        # Building the seam mints a GitHub App token: it reads the PEM, takes a
        # process-global lock and may POST — all blocking, so never on the loop.
        gh = await asyncio.to_thread(self._gh_factory, repo)
        pointer = await self.open_pointer(db, project)
        docs = await self.branch_docs(gh, branch, pointer=pointer)
        if op.op == "create" and any(d.slug == op.slug for d in docs):
            raise MapOpError("slug_taken", _slug_taken_on_branch(op.slug, branch))
        validate_op(op, await self.context(db, project, docs))

        base = await self._default_branch(repo)
        base_sha = await asyncio.to_thread(gh.default_branch_head, base)
        message = commit_message(op, project.name or project.slug or "")

        async def _read_and_apply() -> tuple[str, str, str]:
            """Branch head, doc path and the text to commit, read fresh."""
            # Re-read the branch head even when the row has one: the row may be
            # stale (a merge, or a commit by another writer) and it is cheap.
            head = await asyncio.to_thread(gh.ensure_branch, branch, base_sha)
            path = feature_doc_path(op.slug)
            current, _blob = await asyncio.to_thread(gh.read_file, path, branch)
            if op.op == "create" and current is not None:
                raise MapOpError("slug_taken", _slug_taken_on_branch(op.slug, branch))
            path, text = apply_op(op, current)
            return head, path, text

        head, path, text = await _read_and_apply()
        try:
            sha = await asyncio.to_thread(gh.commit_file, branch, head, path, text, message)
        except GhWriteError as e:
            if not e.conflict:
                raise
            # Someone committed between the read and the write. Re-read the head
            # AND the doc and re-apply the op: `fileChanges.additions` replaces
            # the file wholesale, so committing the text computed at the old head
            # would silently drop their change. A second conflict is the caller's
            # to resolve (409).
            head, path, text = await _read_and_apply()
            sha = await asyncio.to_thread(gh.commit_file, branch, head, path, text, message)

        now = self._now()
        if pointer is None:
            pr = await self._adopt_or_open_pr(gh, branch, base, project)
            # Re-run the lookup immediately before inserting: "one open pointer
            # per product" is enforced by lookup, not by a partial unique index
            # (R8b-5), so two concurrent first edits would otherwise both insert.
            # Everything above is idempotent — the commit is atomic and the PR is
            # adopted, not re-opened — so the loser just reuses the winner's row.
            existing = await open_pointer_row(db, project.id)
            if existing is not None:
                pointer = existing
                pointer.head_sha = sha
                if not pointer.pr_number and pr.number:
                    pointer.pr_number, pointer.pr_url = pr.number, pr.url or None
            else:
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

    async def refresh(
        self, db, project: Project, gh: GhWrite | None = None,
    ) -> Pointer | None:
        """Move the open pointer through the PR's lifecycle (R8b-6).

        ONE ``GET /repos/{r}/pulls/{n}`` per open pointer per pass, read
        directly rather than through the session PR poller (which watches
        session branches, not this one). ``mergeable_state`` supplies the two
        facts the UI acts on: ``behind`` offers Rebase, ``dirty`` raises the
        conflict banner.

        Terminal states: merged ⇒ the docs are on the default branch and the
        next index pass stamps ``commit_sha`` over the PR columns; closed
        unmerged ⇒ the same end state as the UI's Abandon, so the never-merged
        proposals are cleaned up here too (R8b-7) — a PR closed on GitHub would
        otherwise leave them proposed forever.

        A GitHub failure is recorded in ``last_error`` and leaves the status
        alone: the caller is a polling loop, and a transient 502 must not
        abandon a live PR. Returns ``None`` when there is no open pointer.
        """
        pointer = await open_pointer_row(db, project.id)
        if pointer is None:
            return None
        if not pointer.pr_number:
            return Pointer.from_row(pointer)
        repo = pointer.repo or await self._primary_repo(db, project)
        if gh is None:   # the token mint blocks — never on the loop
            gh = await asyncio.to_thread(self._gh_factory, repo)
        try:
            pr = await asyncio.to_thread(gh.read_pr, pointer.pr_number)
        except GhWriteError as e:
            return await self._refresh_failed(db, pointer, f"{e.step}: {e.detail}")
        except subprocess.TimeoutExpired as e:
            # A hung `gh` escapes `_run_gh` rather than becoming a GhWriteError,
            # and it is the same kind of upstream failure: record it and leave
            # the status alone.
            return await self._refresh_failed(db, pointer, f"pr_read: {e}")

        now = self._now()
        pointer.last_error = None
        if pr.head_sha:
            pointer.head_sha = pr.head_sha
        pointer.behind = pr.behind
        pointer.conflicting = pr.conflicting
        if pr.merged:
            pointer.status = MAP_CHANGES_STATUS_MERGED
            pointer.merged_at = _parse_gh_time(pr.merged_at) or now
            pointer.closed_at = pointer.merged_at
            pointer.behind = pointer.conflicting = False
        elif pr.state == "closed":
            pointer.status = MAP_CHANGES_STATUS_ABANDONED
            pointer.closed_at = now
            pointer.behind = pointer.conflicting = False
            await self.cleanup_after_abandon(db, project, pointer.pr_number)
        await db.flush()
        return Pointer.from_row(pointer)

    @staticmethod
    async def _refresh_failed(db, pointer: FeatureMapChange, detail: str) -> Pointer:
        """Record why the PR read failed WITHOUT touching the status: the caller
        is a polling loop and a transient failure must not abandon a live PR."""
        pointer.last_error = detail[:300]
        await db.flush()
        return Pointer.from_row(pointer)

    async def cleanup_after_abandon(
        self, db, project: Project, pr_number: int | None,
    ) -> dict[str, int]:
        """Drop the capabilities that only ever existed on the abandoned branch
        (R8b-7). Returns ``{"deleted": n, "orphaned": m}``.

        A proposed-only row is one the index wrote from the branch head:
        ``pr_number`` is this PR's and ``commit_sha`` is NULL (a merged doc has
        the default-branch SHA — R8b-4). The branch is gone, so its doc will
        never be read again and no index pass can retire the row; without this
        it stays on the map as a proposal forever.

        Rows something else points at are ORPHANED instead of deleted, so no
        reference is left dangling: a classified ``work_items.feature_id`` and
        an ``initiative_features`` membership are decisions a human made against
        that capability. The remaining incoming FKs are not decisions and are
        cleared so the delete is legal on Postgres: ``feature_links`` rows are
        rewritten wholesale by every index pass, a child's ``parent_feature_id``
        is re-resolved by the same pass, and a triage proposal's
        ``proposed_feature_id`` pointed at a capability that never existed."""
        if not pr_number:
            return {"deleted": 0, "orphaned": 0}
        rows = (await db.execute(select(Feature).where(
            Feature.project_id == project.id,
            Feature.pr_number == pr_number,
            Feature.commit_sha.is_(None)))).scalars().all()
        if not rows:
            return {"deleted": 0, "orphaned": 0}
        ids = [r.id for r in rows]
        referenced = {
            fid for (fid,) in (await db.execute(select(WorkItem.feature_id).where(
                WorkItem.feature_id.in_(ids)))).all() if fid}
        referenced |= {
            fid for (fid,) in (await db.execute(select(InitiativeFeature.feature_id).where(
                InitiativeFeature.feature_id.in_(ids)))).all() if fid}
        doomed = [r for r in rows if r.id not in referenced]
        for row in rows:
            if row.id in referenced:
                row.orphaned = True
        if doomed:
            dead = [r.id for r in doomed]
            await db.execute(update(Feature).where(
                Feature.parent_feature_id.in_(dead)).values(parent_feature_id=None))
            await db.execute(delete(FeatureLink).where(
                or_(FeatureLink.src_feature_id.in_(dead),
                    FeatureLink.dst_feature_id.in_(dead))))
            await db.execute(update(TriageProposal).where(
                TriageProposal.proposed_feature_id.in_(dead)).values(
                    proposed_feature_id=None))
            await db.execute(delete(Feature).where(Feature.id.in_(dead)))
        await db.flush()
        counts = {"deleted": len(doomed), "orphaned": len(rows) - len(doomed)}
        log.info("map_changes: abandon cleanup project=%s pr=%s %s",
                 project.slug, pr_number, counts)
        return counts

    async def _adopt_stranded(
        self, db, project: Project,
    ) -> tuple[FeatureMapChange, GhWrite]:
        """A pointer row for a branch/PR that exists on GitHub with no row.

        Same failure as the edit path's adoption (F2): the commit, and then the
        PR, land before the DB row, so a crash in between strands real work
        nothing points at. Without this, Abandon answers 404 forever and the only
        way out is an edit — while the stranded branch's docs keep showing on the
        map through the next index pass.

        Raises :class:`NoOpenMapChanges` when neither the branch nor a PR exists,
        which is the genuine "nothing to abandon" 404."""
        branch = map_changes_branch_name(project.slug or "")
        repo = await self._primary_repo(db, project)
        gh = await asyncio.to_thread(self._gh_factory, repo)   # token mint blocks
        base = await self._default_branch(repo)
        exists = await asyncio.to_thread(gh.branch_exists, branch)
        found = await asyncio.to_thread(gh.find_open_pr, branch, base)
        if found is None and not exists:
            raise NoOpenMapChanges(
                f"product '{project.slug}' has no open map-changes PR")
        pr = (await asyncio.to_thread(gh.read_pr, found.number)
              if found is not None and found.number else found)
        log.info("map_changes: abandoning a stranded %s for %s",
                 "PR" if pr is not None else "branch", branch)
        pointer = FeatureMapChange(
            project_id=project.id, repo=repo, branch=branch,
            pr_number=(pr.number or None) if pr is not None else None,
            pr_url=(pr.url or None) if pr is not None else None,
            status=MAP_CHANGES_STATUS_OPEN,
            head_sha=(pr.head_sha or None) if pr is not None else None,
            behind=bool(pr and pr.behind), conflicting=bool(pr and pr.conflicting),
            opened_at=self._now())
        db.add(pointer)
        await db.flush()
        return pointer, gh

    async def abandon(self, db, project: Project, identity: str) -> Pointer:
        """Close the PR, delete the branch and mark the row abandoned (R8b-7).
        The never-merged proposals it leaves behind are dropped by
        :meth:`cleanup_after_abandon`, which the route calls next.

        With no pointer row the branch/PR are probed directly and adopted
        (:meth:`_adopt_stranded`) — only "neither exists" is a 404."""
        pointer = await self.open_pointer(db, project)
        if pointer is None:
            pointer, gh = await self._adopt_stranded(db, project)
        else:
            repo = pointer.repo or await self._primary_repo(db, project)
            gh = await asyncio.to_thread(self._gh_factory, repo)  # token mint blocks
        if pointer.pr_number:
            await asyncio.to_thread(gh.close_pr, pointer.pr_number)
        if pointer.branch:
            await asyncio.to_thread(gh.delete_branch, pointer.branch)
        pointer.status = MAP_CHANGES_STATUS_ABANDONED
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
        repo = pointer.repo or await self._primary_repo(db, project)
        if not pointer.pr_number:
            raise NotBehind(f"map-changes PR for '{project.slug}' is not behind its base")
        if not pointer.behind:
            # A subsequent poll may already report clean while a previously
            # accepted update still owns its targets. Reconcile that request.
            from server.db.models import AutomationOperation
            pending = await db.scalars(select(AutomationOperation).where(
                AutomationOperation.owner == "map-changes",
                AutomationOperation.phase.in_(("active", "uncertain", "completed"))))
            if not any(op.provenance.get("repo") == repo
                       and op.provenance.get("pr_number") == pointer.pr_number for op in pending):
                raise NotBehind(f"map-changes PR for '{project.slug}' is not behind its base")
        from server.automations.mechanical_update import manual_update
        from server.automations.ownership import OwnershipConflict, StaleOwnership
        from server.automations.target_identity import TargetIdentityError
        gh = await asyncio.to_thread(self._gh_factory, repo)

        async def transport(_repo, args):
            return await asyncio.to_thread(gh.recovery_request, args)

        try:
            outcome = await manual_update(repo, pointer.pr_number, transport=transport,
                                          allow_start=bool(pointer.behind))
        except (OwnershipConflict, StaleOwnership, TargetIdentityError, ValueError) as exc:
            raise GhWriteError("update_branch", str(exc), conflict=True) from exc
        if outcome in ("rejected", "unknown"):
            raise GhWriteError("update_branch", f"GitHub update {outcome}; retry reconciles ownership")
        # Accepted means queued, not completed; the next PR read derives behind.
        await db.flush()
        await record_audit(
            db=db, type="pm.map_changes.rebase", identity=identity,
            detail={"slug": project.slug, "branch": pointer.branch,
                    "prNumber": pointer.pr_number})
        return Pointer.from_row(pointer)
