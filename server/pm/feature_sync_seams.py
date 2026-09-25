"""The two pm-sync feature-index seams that need NO local git clone.

``SessionOrchestrator`` (the daemon) and ``RemoteOrchestrator`` (the edge) both
have to materialize a capability-doc read into the ``Feature`` table — the
former for its periodic pm-sync tick, the latter for ``MapChangesService``'s
synchronous ``_resync_now`` after a retire/merge/triage-inline-create. The work
is pure DB: each function opens its OWN ``db_session()`` and needs nothing from
the orchestrator instance, so it lives here as a module function rather than
being duplicated per orchestrator.

The third seam — READING the docs — is NOT here, because it genuinely differs
by process: the daemon reads a local clone with git plumbing
(``SessionOrchestrator._pm_sync_read_feature_docs``), while the edge has no
clone at all and must go through the GitHub API
(``server.pm.map_changes.read_feature_docs_via_gh_api``).

``SessionOrchestrator._pm_sync_list_feature_rows`` /
``_pm_sync_apply_feature_plan`` are thin wrappers around these, kept so that
every existing bound-method call site and every test that monkeypatches the
instance attribute keeps working.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, func, select

from server.db import db_session
from server.db.models import Feature, FeatureEvidence, FeatureLink, Persona, PersonaFeature, Project
from server.obs.logdedupe import LogThrottle, log_event_throttled

from .feature_index import FeatureIndexPlan, FeatureRowView

log = logging.getLogger(__name__)

# A feature-doc link to a project/feature that doesn't resolve stays unresolved
# on every pm-sync tick until someone edits the doc, so this repeated the same
# warning every 60s tick (#1531, re-filed as #1875 once #1531 was closed with
# the link still broken — same fingerprint, same first-seen timestamp). Keyed
# by the unresolved edge's identity, not the tick. Module-level so the window is
# shared by every caller (daemon tick and edge resync alike).
_PM_SYNC_LINK_LOG = LogThrottle()

# Same shape for the planner's parent warnings: a doc citing a parent that is
# not on the default branch stays that way every tick until the map changes,
# and the old interpolated line filed one issue per child slug (#2242/#2243).
_PM_SYNC_PARENT_LOG = LogThrottle()


async def list_feature_rows(project_id: str) -> list[FeatureRowView]:
    """Return FeatureRowView for every Feature belonging to *project_id*."""
    async with db_session() as db:
        rows = (await db.execute(
            select(Feature).where(Feature.project_id == project_id)
        )).scalars().all()
    return [FeatureRowView(slug=f.slug or "", orphaned=bool(f.orphaned))
            for f in rows]


async def apply_feature_plan(project_id: str, plan: FeatureIndexPlan) -> None:
    """Write upserts/orphans from *plan* into the Feature table, then the
    capability tree (parent) and the project's outgoing feature links.

    Parents resolve by slug within the project after every upsert row
    exists, so a child listed before its parent still links. Links are
    REWRITTEN each apply — the docs are truth, so a link that left the
    frontmatter leaves the table — and resolve by (project slug, Feature.slug),
    where the project slug may also be one the target has been renamed away
    from (``server/pm/project_slugs.py``) and an omitted one means this
    project; an unresolvable target is skipped
    with a warning rather than failing the apply. Plan warnings (unknown
    parent, cycle) are logged the same way.

    After upserting, if the project is in ``map_proposed``, ``registered``,
    or ``syncing`` and now has ≥1 non-orphaned Feature, advance it to
    ``map_merged``.  Indexed docs from the default branch ARE a merged map
    regardless of onboarding route.  ``map_merged``, ``classifying``, and
    ``live`` are left alone."""
    from server.pm.project_slugs import resolve_project_slugs
    for warning in plan.warnings:
        log_event_throttled(
            log, _PM_SYNC_PARENT_LOG,
            f"{project_id}\0{warning.kind}\0{warning.slug}\0{warning.parent}",
            logging.WARNING, f"pm_sync.feature_{warning.kind}",
            project=project_id, slug=warning.slug, parent=warning.parent)
    async with db_session() as db:
        for up in plan.upserts:
            row = (await db.execute(
                select(Feature).where(
                    Feature.project_id == project_id,
                    Feature.slug == up.slug,
                )
            )).scalar_one_or_none()
            if row is None:
                row = Feature(project_id=project_id, slug=up.slug)
                db.add(row)
            row.title = up.title
            row.state = up.state
            row.summary = up.summary
            row.repo_path = up.repo_path
            row.orphaned = False
            row.commit_sha = up.commit_sha
            row.pr_number = up.pr_number
            row.pr_url = up.pr_url
            row.route_labels = list(up.labels) or None
            row.persona_slugs = list(up.personas) or None
            if up.doc_committed_at is not None:
                # None = this read could not tell (the edge's GitHub-API read);
                # keep the last clone-backed answer rather than erasing it.
                row.doc_committed_at = up.doc_committed_at
        for slug in plan.orphan_slugs:
            row = (await db.execute(
                select(Feature).where(
                    Feature.project_id == project_id,
                    Feature.slug == slug,
                )
            )).scalar_one_or_none()
            if row is not None:
                row.orphaned = True
        await db.flush()

        # Tree: parent by slug, within this project only.
        mine = {f.slug: f for f in (await db.execute(
            select(Feature).where(Feature.project_id == project_id))).scalars().all()}
        for up in plan.upserts:
            row = mine.get(up.slug)
            if row is None:
                continue
            parent = mine.get(up.parent_slug) if up.parent_slug else None
            row.parent_feature_id = parent.id if parent is not None else None

        # Evidence (#2162): rewritten per upserted feature — the doc is truth —
        # but a path this pass left unresolved inherits its last resolution, so
        # an edge resync (no clone) does not blank what the daemon learned.
        up_ids = [mine[up.slug].id for up in plan.upserts if up.slug in mine]
        if up_ids:
            prior = {(e.feature_id, e.kind, e.path): e for e in (await db.execute(
                select(FeatureEvidence).where(
                    FeatureEvidence.feature_id.in_(up_ids)))).scalars().all()}
            await db.execute(delete(FeatureEvidence).where(
                FeatureEvidence.feature_id.in_(up_ids)))
            for up in plan.upserts:
                feat = mine.get(up.slug)
                if feat is None:
                    continue
                for ev in up.evidence:
                    old = prior.get((feat.id, ev.kind, ev.path))
                    missing, when = ev.missing, ev.last_commit_at
                    if missing is None and old is not None:
                        missing, when = old.missing, old.last_commit_at
                    db.add(FeatureEvidence(feature_id=feat.id, kind=ev.kind, path=ev.path,
                                           missing=missing, last_commit_at=when))

        # Personas (#2164): rewritten per upserted feature, resolved WITHIN
        # this project only (unlike FeatureLink, personas are not cross-
        # project) — a declared slug that doesn't resolve to a live Persona
        # row is simply dropped from the link table; `persona_slugs` above
        # still carries the raw declaration for roadmap_health to flag.
        if up_ids:
            await db.execute(delete(PersonaFeature).where(
                PersonaFeature.feature_id.in_(up_ids)))
            personas_by_slug = {p.slug: p.id for p in (await db.execute(
                select(Persona).where(
                    Persona.project_id == project_id, Persona.orphaned.isnot(True),
                ))).scalars().all()}
            for up in plan.upserts:
                feat = mine.get(up.slug)
                if feat is None:
                    continue
                for pslug in dict.fromkeys(up.personas):
                    pid = personas_by_slug.get(pslug)
                    if pid is not None:
                        db.add(PersonaFeature(persona_id=pid, feature_id=feat.id))

        # Links: rewrite every outgoing edge of this project's features.
        src_ids = [f.id for f in mine.values()]
        if src_ids:
            await db.execute(delete(FeatureLink).where(FeatureLink.src_feature_id.in_(src_ids)))
        wanted = list(dict.fromkeys(
            (up.slug, link) for up in plan.upserts for link in up.links))
        if wanted:
            # Through project_slugs, not a bare Project.slug lookup: a doc in
            # another repo may still name a product by a slug it has since
            # been renamed away from, and this block DELETES and rebuilds
            # every outgoing edge, so an unresolved target is edge loss, not
            # just a warning (#1881).
            projects = await resolve_project_slugs(
                db, {link.project for _, link in wanted})
            # A link with no `project:` names a feature of this doc's own
            # product, as `parent:` always does (#2257) — `""` is not a slug.
            projects[""] = project_id
            targets = {(f.project_id, f.slug): f.id for f in (await db.execute(
                select(Feature).where(Feature.project_id.in_(list(projects.values()))))).scalars().all()}
            for src_slug, link in wanted:
                src = mine.get(src_slug)
                dst_project = projects.get(link.project)
                dst_id = targets.get((dst_project, link.feature)) if dst_project else None
                if src is None or dst_id is None:
                    # Structured, not interpolated: the slugs below are the
                    # only thing that differs between one broken edge and
                    # the next, and in a plain message they land in the
                    # error-sink fingerprint — one filed issue per edge
                    # (#1533/#1877 vs #1531/#1875). The throttle key stays
                    # per-edge so a newly broken edge still reports at once.
                    log_event_throttled(
                        log, _PM_SYNC_LINK_LOG,
                        f"{project_id}\0{src_slug}\0{link.project}\0{link.feature}",
                        logging.WARNING, "pm_sync.feature_link_unresolved",
                        project=project_id, src=src_slug,
                        dst_project=link.project, dst_feature=link.feature)
                    continue
                db.add(FeatureLink(src_feature_id=src.id, dst_feature_id=dst_id, kind=link.kind))

        # Advance map_proposed/registered/syncing → map_merged when ≥1 non-orphaned
        # feature exists after this upsert. Indexed docs ARE a merged map.
        proj = (await db.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one_or_none()
        if proj is not None and proj.onboarding_state in ("map_proposed", "registered", "syncing"):
            # Only features indexed from the default branch (commit_sha set)
            # count — a proposed feature (an open map-changes PR, not yet
            # merged) must not itself flip onboarding to map_merged.
            live_count = (await db.execute(
                select(func.count(Feature.id)).where(
                    Feature.project_id == project_id,
                    Feature.orphaned.is_not(True),
                    Feature.commit_sha.isnot(None),
                )
            )).scalar_one()
            if live_count >= 1:
                proj.onboarding_state = "map_merged"
