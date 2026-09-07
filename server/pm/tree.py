"""Pure roadmap-tree rules (spec 2026-09-07 §10-§11): one attention tier per node,
rolled up from children, and the nested product → capability → initiative shape
the web renders. No I/O — `server/api/pm_tree.py` loads rows and calls this.

The tier is computed HERE, once, for every node; the web's `roadmapTier.ts` only
maps these names to tones (constitution VI).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from server.db.models import INITIATIVE_STAGES
from server.pm.desired_state import ACTIVE_SESSION_STATUSES, HUMAN_WAIT_SESSION_STATUSES

TIERS: tuple[str, ...] = ("needs-you", "blocked", "in-flight", "proposed", "live", "idle")
_RANK = {t: i for i, t in enumerate(TIERS)}
_IN_FLIGHT_STAGES = frozenset(INITIATIVE_STAGES[:INITIATIVE_STAGES.index("deploy") + 1])


def max_tier(*tiers: str) -> str:
    """The highest-priority tier among *tiers*; unknown strings count as idle."""
    best = "idle"
    for t in tiers:
        if _RANK.get(t, len(TIERS)) < _RANK[best]:
            best = t
    return best


def initiative_tier(status: str | None, stage: str | None,
                    epic_blocked: int, epic_needs_human: int) -> str:
    if status == "awaiting_human" or epic_needs_human > 0:
        return "needs-you"
    if status == "blocked" or epic_blocked > 0:
        return "blocked"
    if status == "done" or stage == "done":
        return "live"
    if status == "proposed":
        return "proposed"
    if status == "active" and stage in _IN_FLIGHT_STAGES:
        return "in-flight"
    return "idle"


def capability_own_tier(state: str | None) -> str:
    if state == "live":
        return "live"
    if state == "proposed":
        return "proposed"
    return "idle"


def issue_tier(state: str | None, triage_state: str | None,
               has_open_blocker: bool, session_status: str | None) -> str:
    if state in ("closed", "merged"):
        return "live"
    if triage_state == "needs_human" or session_status in HUMAN_WAIT_SESSION_STATUSES:
        return "needs-you"
    if has_open_blocker:
        return "blocked"
    if session_status in ACTIVE_SESSION_STATUSES:
        return "in-flight"
    return "idle"


def incident_tier(status: str | None) -> str:
    if status in ("open", "scheduled"):
        return "needs-you"
    if status in ("diagnosing", "queued"):
        return "in-flight"
    return "idle"


@dataclass(frozen=True)
class CapabilityIn:
    id: str
    slug: str
    title: str | None
    state: str | None
    summary: str | None
    doc_path: str | None
    commit_sha: str | None
    pr_number: int | None
    parent_slug: str | None
    issue_tiers: tuple[str, ...]          # one tier per OPEN issue classified under it
    links: tuple[tuple[str, str, str], ...] = ()   # (kind, project slug, feature slug)


@dataclass(frozen=True)
class InitiativeIn:
    slug: str
    title: str | None
    stage: str | None
    status: str | None
    primary_slug: str | None
    capability_slugs: tuple[str, ...]
    epic: dict | None                     # {ref,url,total,done,blocked,needsHuman} or None


@dataclass(frozen=True)
class ProductIn:
    slug: str
    name: str | None
    capabilities: tuple[CapabilityIn, ...]
    initiatives: tuple[InitiativeIn, ...]
    awaiting_triage: int
    needs_home: int
    incident_statuses: tuple[str, ...] = field(default_factory=tuple)


def _initiative_node(ini: InitiativeIn, project_slug: str) -> dict:
    epic = ini.epic or {}
    return {
        "slug": ini.slug, "title": ini.title, "stage": ini.stage, "status": ini.status,
        "projectSlug": project_slug, "primaryCapability": ini.primary_slug,
        "capabilities": list(ini.capability_slugs),
        "tier": initiative_tier(ini.status, ini.stage,
                                int(epic.get("blocked", 0)), int(epic.get("needsHuman", 0))),
        "epic": ini.epic,
    }


def _empty_counts() -> dict:
    return {"openIssues": 0, "inFlight": 0, "blocked": 0, "needsYou": 0}


def _add_counts(into: dict, other: dict) -> None:
    for k in ("openIssues", "inFlight", "blocked", "needsYou"):
        into[k] += other[k]


def _bump(counts: dict, tier: str) -> None:
    """Tally *tier* into inFlight/blocked/needsYou; a no-op for any other tier."""
    if tier == "in-flight":
        counts["inFlight"] += 1
    elif tier == "blocked":
        counts["blocked"] += 1
    elif tier == "needs-you":
        counts["needsYou"] += 1


def _capability_node(cap: CapabilityIn, children: list[dict],
                     initiatives_here: list[dict], touching: list[str]) -> dict:
    counts = _empty_counts()
    counts["openIssues"] = len(cap.issue_tiers)
    counts["blocked"] = sum(1 for t in cap.issue_tiers if t == "blocked")
    counts["needsYou"] = sum(1 for t in cap.issue_tiers if t == "needs-you")
    counts["inFlight"] = sum(1 for t in cap.issue_tiers if t == "in-flight")
    for ini in initiatives_here:
        _bump(counts, ini["tier"])
    for child in children:
        _add_counts(counts, child["counts"])
    tier = max_tier(capability_own_tier(cap.state), *cap.issue_tiers,
                    *(c["tier"] for c in children), *(i["tier"] for i in initiatives_here))
    return {
        "id": cap.id, "slug": cap.slug, "title": cap.title, "state": cap.state,
        "summary": cap.summary, "docPath": cap.doc_path, "commitSha": cap.commit_sha,
        "prNumber": cap.pr_number, "parentSlug": cap.parent_slug, "tier": tier,
        "counts": counts,
        "links": [{"kind": k, "projectSlug": p, "featureSlug": f} for k, p, f in cap.links],
        "initiatives": touching,
        "children": children,
    }


def build_product_tree(product: ProductIn) -> dict:
    caps = {c.slug: c for c in product.capabilities}
    children_of: dict[str | None, list[CapabilityIn]] = {}
    for c in product.capabilities:
        parent = c.parent_slug if c.parent_slug in caps else None
        children_of.setdefault(parent, []).append(c)

    initiative_nodes = [_initiative_node(i, product.slug) for i in product.initiatives]
    primary_under: dict[str, list[dict]] = {}
    touching: dict[str, list[str]] = {}
    for node in initiative_nodes:
        if node["primaryCapability"] in caps:
            primary_under.setdefault(node["primaryCapability"], []).append(node)
        for slug in node["capabilities"]:
            if slug in caps:
                touching.setdefault(slug, []).append(node["slug"])

    def build(cap: CapabilityIn) -> dict:
        kids = [build(k) for k in sorted(children_of.get(cap.slug, []), key=lambda c: c.slug)]
        return _capability_node(cap, kids, primary_under.get(cap.slug, []), touching.get(cap.slug, []))

    roots = [build(c) for c in sorted(children_of.get(None, []), key=lambda c: c.slug)]
    # Product-level counts have their own shape (no openIssues) — do not reuse the
    # capability-shaped _empty_counts()/_add_counts() helpers here.
    counts = {"inFlight": 0, "blocked": 0, "needsYou": 0}
    for r in roots:
        counts["inFlight"] += r["counts"]["inFlight"]
        counts["blocked"] += r["counts"]["blocked"]
        counts["needsYou"] += r["counts"]["needsYou"]
    for node in initiative_nodes:
        if node["primaryCapability"] not in caps:
            _bump(counts, node["tier"])
    counts["capabilities"] = len(product.capabilities)
    counts["awaitingTriage"] = product.awaiting_triage
    counts["needsHome"] = product.needs_home
    pseudo = "needs-you" if (product.awaiting_triage or product.needs_home) else "idle"
    tier = max_tier(pseudo, *(r["tier"] for r in roots), *(i["tier"] for i in initiative_nodes),
                    *(incident_tier(s) for s in product.incident_statuses))
    return {"slug": product.slug, "name": product.name, "tier": tier, "counts": counts,
            "capabilities": roots, "initiatives": initiative_nodes}
