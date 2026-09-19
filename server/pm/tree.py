"""Pure roadmap-tree rules (spec 2026-09-07 §10-§11): one attention tier per node,
rolled up from children, and the nested product → capability → initiative shape
the web renders. No I/O — `server/api/pm_tree.py` loads rows and calls this.

The tier is computed HERE, once, for every node; the web's `roadmapTier.ts` only
maps these names to tones (constitution VI).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from server.db.models import INITIATIVE_STAGES
from server.pm.desired_state import (
    ACTIVE_SESSION_STATUSES,
    FAILED_TURN_SESSION_STATUSES,
    HELD_KINDS,
    HUMAN_WAIT_SESSION_STATUSES,
    STATUS_BLOCKED,
    STATUS_IN_PROGRESS,
    is_held_status,
)
from server.pm.outcomes import bounced_out
from server.sdlc.gates import auto_resolved_view

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


def initiative_reason(status: str | None, stage: str | None,
                      epic_total: int, epic_done: int, *,
                      has_children: bool = False) -> str | None:
    """Why an initiative needs a human beyond its status: today only "advance" —
    the build epic is fully closed but nobody has moved the gate (finish-line
    spec §4.5). No auto-advance: SDLC governance keeps the human as the gate.

    A program (``has_children``) never "advances" itself — it is advanced by
    advancing its phases one at a time."""
    if has_children:
        return None
    if status == "active" and stage == "build" and epic_total > 0 and epic_done >= epic_total:
        return "advance"
    return None


def initiative_blocked_reason(status: str | None, epic_blocked: int) -> str | None:
    """Why a `blocked`-tier initiative is blocked — the disposition differs.

    ``"blocked"`` is the row's OWN status, written by a failed materialize apply
    (`server/pm/materialize_apply.py:fail`) or by the synthesizer giving up on a
    plan after `MATERIALIZE_MAX_ATTEMPTS` decompositions
    (`server/automations/sdlc_synthesizer.py:_materialize`) — the unblock route
    clears the first and refuses the second. ``"epic_blocked"`` means ≥1
    sub-issue of its epic carries an open
    blocked-by dependency, cleared by removing the dependency or closing the
    blocker. The row's own status wins: the human clears the row first."""
    if status == "blocked":
        return "blocked"
    if epic_blocked > 0:
        return "epic_blocked"
    return None


def initiative_tier(status: str | None, stage: str | None,
                    epic_blocked: int, epic_needs_human: int, *,
                    epic_total: int = 0, epic_done: int = 0,
                    has_children: bool = False) -> str:
    if status == "awaiting_human" or epic_needs_human > 0:
        return "needs-you"
    if initiative_reason(status, stage, epic_total, epic_done,
                         has_children=has_children) is not None:
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


def issue_reason(*, state: str | None = None, bounces: int = 0,
                 give_up_after_bounces: int = 0) -> str | None:
    """Why an issue needs a human beyond its tier — the ``InitiativeIn.reason``
    shape. ``needs_spec`` when D8's rule fires (ONE rule: ``bounced_out``).

    A RESOLVED issue (``state`` closed/merged) carries no reason at all: it takes
    ``issue_tier``'s own closed/merged short-circuit to ``live``, and a reason
    surviving that renders "bounced N× — needs spec" on a done leaf — the tier and
    the chip disagreeing about one row. The bounce COUNT is untouched (the history
    happened); only the call to action goes."""
    if state in ("closed", "merged"):
        return None
    return "needs_spec" if bounced_out(bounces, give_up_after_bounces) else None


def issue_tier(state: str | None, triage_state: str | None,
               has_open_blocker: bool, session_status: str | None,
               board_status: str | None = None, *,
               reason: str | None = None, hold_kind: str | None = None) -> str:
    # triage_state stays in the signature (callers pass it positionally) but is no
    # longer read for needs-you — that is the hold's job now (spec 2026-09-18 §7).
    if state in ("closed", "merged"):
        return "live"
    if reason is not None:
        return "needs-you"
    if (hold_kind in HELD_KINDS or session_status in HUMAN_WAIT_SESSION_STATUSES
            or is_held_status(board_status)):
        return "needs-you"
    if has_open_blocker or board_status == STATUS_BLOCKED:
        return "blocked"
    if (session_status in (ACTIVE_SESSION_STATUSES | FAILED_TURN_SESSION_STATUSES)
            or board_status == STATUS_IN_PROGRESS):
        return "in-flight"
    return "idle"


def incident_tier(status: str | None) -> str:
    if status in ("open", "scheduled"):
        return "needs-you"
    if status in ("diagnosing", "queued"):
        return "in-flight"
    return "idle"


def _acyclic_parents(parent_of: dict[str, str | None]) -> dict[str, str | None]:
    """*parent_of* with every unknown or cyclic parent link severed to
    ``None`` — "an unknown or cyclic parent counts as a root" (the nesting
    plan's rule), computed once so every downstream walker
    (``rollup_epic_progress``'s leaf recursion, ``build_product_tree``'s depth
    and phase grouping) can trust the result is already a forest, never a
    graph with cycles."""
    out: dict[str, str | None] = {}
    for iid, parent in parent_of.items():
        if parent is not None and parent not in parent_of:
            parent = None
        if parent is not None:
            seen = {iid}
            cur: str | None = parent
            while cur is not None and cur not in seen:
                seen.add(cur)
                cur = parent_of.get(cur)
                if cur is not None and cur not in parent_of:
                    cur = None
            if cur is not None:      # loop only exits early on a revisit → cyclic
                parent = None
        out[iid] = parent
    return out


def rollup_epic_progress(entries: dict[str, dict | None],
                         parent_of: dict[str, str | None]) -> dict[str, dict | None]:
    """A program's progress is the sum over its LEAF descendants' epics — the
    issues people close — never a count of its sub-epics. Leaves pass through.
    A program without an epic entry of its own gets one (ref/url None). A
    program's `priority` is the max over its leaves (spec 2026-09-14 §10)."""
    parent_of = _acyclic_parents(parent_of)
    children: dict[str, list[str]] = {}
    for cid, pid in parent_of.items():
        if pid is not None:
            children.setdefault(pid, []).append(cid)

    def leaves(iid: str, seen: frozenset[str] = frozenset()) -> list[str]:
        if iid in seen:  # defensive second guard: parent_of is already acyclic,
            return []    # but recursion should never trust that alone.
        kids = children.get(iid, [])
        if not kids:
            return [iid]
        return [leaf for k in kids for leaf in leaves(k, seen | {iid})]

    out: dict[str, dict | None] = {}
    for iid, entry in entries.items():
        if iid not in children:
            out[iid] = entry
            continue
        totals = {"total": 0, "done": 0, "blocked": 0, "held": 0}
        priorities: list[float] = []
        for leaf in leaves(iid):
            e = entries.get(leaf)
            if e:
                for k in totals:
                    totals[k] += int(e.get(k) or 0)
                if e.get("priority") is not None:
                    priorities.append(float(e["priority"]))
        base = entry or {"ref": None, "url": None}
        out[iid] = {"ref": base.get("ref"), "url": base.get("url"), **totals,
                    "priority": max(priorities) if priorities else None}
    return out


def derived_stage(stages: list[str | None]) -> str | None:
    """A program sits where its slowest phase sits: the earliest non-done
    stage among its phases; ``done`` only when every RECOGNIZED phase stage is
    done and at least one is recognized; ``None`` with no phases, or when none
    of the phases' stages is a recognized ``INITIATIVE_STAGES`` value (the
    caller then falls back to the row's own stage rather than a phantom
    "done")."""
    if not stages:
        return None
    recognized = [s for s in stages if s in INITIATIVE_STAGES]
    if not recognized:
        return None
    live = [s for s in recognized if s != "done"]
    if not live:
        return "done"
    return min(live, key=INITIATIVE_STAGES.index)


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
    epic: dict | None                     # {ref,url,total,done,blocked,held} or None
    parent_slug: str | None = None        # program this phase nests under, or None
    # `Initiative.last_auto_gate` verbatim (spec 2026-09-14 §8): what the
    # synthesizer last did with one of this row's gates, or None. Rendered as
    # `autoResolved` only inside the visible window (`auto_resolved_view`).
    last_auto_gate: dict | None = None


@dataclass(frozen=True)
class ProductIn:
    slug: str
    name: str | None
    capabilities: tuple[CapabilityIn, ...]
    initiatives: tuple[InitiativeIn, ...]
    awaiting_triage: int
    needs_home: int
    incident_statuses: tuple[str, ...] = field(default_factory=tuple)
    # The product's OPEN map-changes pointer as `Pointer.to_dict()` (spec §9),
    # or None. Carried through verbatim: the shape is the service's, and the
    # product header renders it (open PR, its age, Rebase while behind).
    map_changes: dict | None = None


def _initiative_node(ini: InitiativeIn, project_slug: str, *,
                     has_children: bool = False,
                     parent_slug: str | None = None) -> dict:
    epic = ini.epic or {}
    total, done = int(epic.get("total", 0)), int(epic.get("done", 0))
    # NOTE: this node's `primaryCapability` is a slug string (the map tree is
    # slug-keyed), whereas server/api/sdlc.py's initiative payload serializes
    # `primaryCapability` as `{id, slug, title}`. Both shapes are intended —
    # Phase 3 consumes each from its own endpoint — not a drift to reconcile.
    epic_blocked, epic_needs = int(epic.get("blocked", 0)), int(epic.get("held", 0))
    tier = initiative_tier(ini.status, ini.stage, epic_blocked, epic_needs,
                           epic_total=total, epic_done=done, has_children=has_children)
    reason = initiative_reason(ini.status, ini.stage, total, done, has_children=has_children)
    if reason is None and tier == "blocked":
        reason = initiative_blocked_reason(ini.status, epic_blocked)
    return {
        "slug": ini.slug, "title": ini.title, "stage": ini.stage, "status": ini.status,
        "projectSlug": project_slug, "primaryCapability": ini.primary_slug,
        "capabilities": list(ini.capability_slugs),
        "tier": tier,
        "reason": reason,
        "epic": ini.epic,
        "priority": epic.get("priority"),
        # What the machine did with this row's gate, while the chip is still
        # worth showing (spec D5: 24 h). The reverse action named here is the
        # existing human control (Step back), never a new undo path.
        "autoResolved": auto_resolved_view(ini.last_auto_gate, datetime.now(timezone.utc)),
        # `parent_slug` here is the caller's already-sanitized parent (never
        # `ini.parent_slug` directly) so a cyclic/unknown pointer never leaks
        # through as a phantom `parentInitiative`.
        "parentInitiative": parent_slug,
        "hasChildren": has_children,
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
        if not ini["hasChildren"]:
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

    by_slug = {i.slug: i for i in product.initiatives}
    # One sanitized parent map for the whole build: an unknown OR cyclic
    # parent_slug is severed to None here, once, so has_children/phases_of/
    # depth/parentInitiative all agree a cyclic pair is two independent roots
    # rather than each treating the other as its program.
    parent_of = _acyclic_parents({i.slug: i.parent_slug for i in product.initiatives})
    has_children = {p for p in parent_of.values() if p is not None}

    def _depth(slug: str) -> int:
        seen = {slug}
        depth = 0
        cur = parent_of.get(slug)
        while cur is not None and cur not in seen:
            seen.add(cur)
            depth += 1
            cur = parent_of.get(cur)
        return depth

    phases_of: dict[str, list[str]] = {}
    for slug, parent in parent_of.items():
        if parent is not None:
            phases_of.setdefault(parent, []).append(slug)

    # Build leaves before the programs that roll them up: deepest slug first.
    built: dict[str, dict] = {}
    for slug in sorted(by_slug, key=_depth, reverse=True):
        ini = by_slug[slug]
        is_program = slug in has_children
        node = _initiative_node(ini, product.slug, has_children=is_program,
                                parent_slug=parent_of.get(slug))
        if is_program:
            phase_tiers = [built[p]["tier"] for p in phases_of.get(slug, []) if p in built]
            node["tier"] = max_tier(node["tier"], *phase_tiers)
        built[slug] = node
    initiative_nodes = [built[i.slug] for i in product.initiatives]

    primary_under: dict[str, list[dict]] = {}
    touching: dict[str, list[str]] = {}
    for node in initiative_nodes:
        if node["primaryCapability"] in caps:
            primary_under.setdefault(node["primaryCapability"], []).append(node)
        for slug in node["capabilities"]:
            if slug in caps:
                touching.setdefault(slug, []).append(node["slug"])

    seen: set[str] = set()

    def build(cap: CapabilityIn) -> dict:
        if cap.slug in seen:
            # Defensive: a parent cycle should never reach here (feature-index
            # breaks cycles at index time), but a stale/handcrafted tree must
            # not blow the stack — cut recursion and render a childless node.
            return _capability_node(cap, [], primary_under.get(cap.slug, []), touching.get(cap.slug, []))
        seen.add(cap.slug)
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
        if node["primaryCapability"] not in caps and not node["hasChildren"]:
            _bump(counts, node["tier"])
    counts["capabilities"] = len(product.capabilities)
    counts["awaitingTriage"] = product.awaiting_triage
    counts["needsHome"] = product.needs_home
    pseudo = "needs-you" if (product.awaiting_triage or product.needs_home) else "idle"
    tier = max_tier(pseudo, *(r["tier"] for r in roots), *(i["tier"] for i in initiative_nodes),
                    *(incident_tier(s) for s in product.incident_statuses))
    return {"slug": product.slug, "name": product.name, "tier": tier, "counts": counts,
            "capabilities": roots, "initiatives": initiative_nodes,
            "mapChanges": product.map_changes}
