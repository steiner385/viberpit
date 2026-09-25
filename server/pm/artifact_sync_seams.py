"""pm-sync's artifact-frontmatter DB seam (issue #2161).

Mirrors ``feature_sync_seams.py``: reading a committed artifact's text is a
git-plumbing concern that genuinely differs by process (the daemon's shared
clone vs. the edge's GitHub API), so it stays a per-process seam
(``SessionOrchestrator._pm_sync_read_repo_file``). Everything here is pure DB
work — listing which artifacts need a re-check, and writing the parsed result
back — so it lives as module functions any orchestrator can wire directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from server.db import db_session
from server.db.models import Feature, Initiative, InitiativeArtifact, InitiativeFeature
from server.obs.logdedupe import LogThrottle, log_event_throttled

log = logging.getLogger(__name__)

# An artifact citing a capability slug with no matching Feature repeats every
# pm-sync tick until the doc or the map changes — same shape as
# `feature_sync_seams._PM_SYNC_LINK_LOG` and for the same reason (#1531/#1875).
_UNKNOWN_CAPABILITY_LOG = LogThrottle()


@dataclass(frozen=True)
class ArtifactRowView:
    artifact_id: str
    initiative_id: str
    initiative_slug: str
    kind: str
    repo_path: str
    epic_repo: str | None
    epic_number: int | None


@dataclass(frozen=True)
class ArtifactFrontmatterResult:
    """One artifact's frontmatter re-check, ready to apply. *epic* is the
    verbatim ``"owner/repo#N"`` string; *capabilities* the verbatim slug list.
    A failed read carries no information to write back, so the caller
    (``PmSyncLoop._artifact_frontmatter_sync``) never constructs one for a
    read it couldn't complete — that artifact is simply left out of the batch."""
    artifact_id: str
    initiative_id: str
    frontmatter_present: bool
    epic: str | None
    capabilities: tuple[str, ...]


async def list_committed_artifacts(project_id: str, repo: str) -> list[ArtifactRowView]:
    """Every committed artifact (``repo_path``/``commit_sha`` both set)
    belonging to one of *project_id*'s initiatives IN *repo* — the set
    pm-sync re-reads for frontmatter each pass. Scoped to *repo* because the
    caller (pm-sync's per-primary-repo pass) only has a clone of that one
    repo to read from; an initiative filed against a different repo in the
    same product is left for that repo's own pass. An artifact with no
    commit yet has nothing to read, so it is excluded rather than flagged as
    missing frontmatter."""
    async with db_session() as db:
        rows = (await db.execute(
            select(InitiativeArtifact, Initiative.slug, Initiative.epic_repo, Initiative.epic_number)
            .join(Initiative, Initiative.id == InitiativeArtifact.initiative_id)
            .where(Initiative.project_id == project_id,
                   Initiative.repo == repo,
                   InitiativeArtifact.repo_path.is_not(None),
                   InitiativeArtifact.commit_sha.is_not(None))
        )).all()
    return [
        ArtifactRowView(
            artifact_id=art.id, initiative_id=art.initiative_id,
            initiative_slug=slug or "", kind=art.kind or "",
            repo_path=art.repo_path or "", epic_repo=epic_repo, epic_number=epic_number,
        )
        for art, slug, epic_repo, epic_number in rows
    ]


def _split_epic(epic: str) -> tuple[str, int]:
    """``"owner/repo#123"`` → ``("owner/repo", 123)``. Caller has already
    validated the shape (``artifactdoc._EPIC_RE``)."""
    repo, _, number = epic.partition("#")
    return repo, int(number)


async def apply_artifact_frontmatter(project_id: str, results: list[ArtifactFrontmatterResult]) -> None:
    """Write a frontmatter re-check batch: stamp each artifact row, then — for
    an artifact whose frontmatter states an ``epic`` or ``capabilities`` —
    overwrite the owning Initiative's epic pointer / capability links
    (file wins, per the issue: "like a human board edit wins over the
    reconciler"). An unstated field is left alone; only a STATED field
    overrides the DB, same posture as ``featuredoc``'s reading rule."""
    async with db_session() as db:
        for r in results:
            art = await db.get(InitiativeArtifact, r.artifact_id)
            if art is None:
                continue
            art.frontmatter_present = r.frontmatter_present
            art.frontmatter_epic = r.epic
            art.frontmatter_capabilities = list(r.capabilities) or None

            ini = await db.get(Initiative, r.initiative_id)
            if ini is None:
                continue

            if r.epic:
                epic_repo, epic_number = _split_epic(r.epic)
                if ini.epic_repo != epic_repo or ini.epic_number != epic_number:
                    ini.epic_repo = epic_repo
                    ini.epic_number = epic_number

            if r.capabilities:
                wanted = (await db.execute(select(Feature.id, Feature.slug).where(
                    Feature.project_id == project_id,
                    Feature.slug.in_(r.capabilities),
                    Feature.orphaned.isnot(True),
                ))).all()
                wanted_ids = {fid for fid, _ in wanted}
                found_slugs = {slug for _, slug in wanted}
                for missing in set(r.capabilities) - found_slugs:
                    log_event_throttled(
                        log, _UNKNOWN_CAPABILITY_LOG, f"{r.initiative_id}\0{missing}",
                        logging.WARNING, "pm_sync.artifact_unknown_capability",
                        initiative_id=r.initiative_id, capability=missing)

                current = (await db.execute(select(InitiativeFeature).where(
                    InitiativeFeature.initiative_id == r.initiative_id))).scalars().all()
                current_by_feature = {row.feature_id: row for row in current}
                for row in current:
                    if row.feature_id not in wanted_ids:
                        await db.delete(row)
                for fid in wanted_ids:
                    if fid not in current_by_feature:
                        db.add(InitiativeFeature(initiative_id=r.initiative_id, feature_id=fid,
                                                 is_primary=False))
