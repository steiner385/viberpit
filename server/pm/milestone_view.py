"""Release-milestone view model (spec 2026-11-05 §9–§10): the payload
``GET /api/pm/projects/{slug}/milestones`` serves and the web page renders.

Pure code over already-loaded rows. Freshness is applied here, at read time
(``milestone_status.status_at``): a stored ``met`` older than its measure's max age
reads ``unknown``. Conditions come from the ladder's stored definition; a measurement
row no condition names is ignored. Product-agnostic.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from server.pm.milestone_def import Condition, condition_from_json, from_json
from server.pm.milestone_github import DONE_REF_STATES
from server.pm.milestone_refresh import LEAVES_KEY, leaves_scope
from server.pm.milestone_status import effective_max_age, gate_met, status_at
from server.time_iso import aware_utc, iso_utc

LEAF_STATES: tuple[str, ...] = ("done", "in_flight", "not_started")


def leaf_state(item: Mapping[str, Any]) -> str:
    """``done`` when the issue/PR is closed or merged; ``in_flight`` when open with
    an open PR (refresh keeps only OPEN closing PRs); else ``not_started``."""
    if item.get("state") in DONE_REF_STATES:
        return "done"
    return "in_flight" if item.get("prs") else "not_started"


def _ref_url(ref: str) -> str:
    repo, _, n = ref.partition("#")
    return f"https://github.com/{repo}/issues/{n}"


def _age_s(measured_at: datetime | None, now: datetime) -> int | None:
    at = aware_utc(measured_at)
    return None if at is None else max(0, int((now - at).total_seconds()))


def _condition(cond: Condition, row: Any | None, now: datetime) -> dict[str, Any]:
    m = cond.measure
    stored = (row.status if row is not None else None) or "unknown"
    measured_at = row.measured_at if row is not None else None
    return {
        "key": cond.key,
        "label": cond.label,
        "kind": m.kind,
        "status": status_at(stored, measured_at, effective_max_age(m), now),
        "value": row.value if row is not None else None,
        "target": m.target,
        "op": m.op,
        "measuredAt": iso_utc(measured_at),
        "ageSeconds": _age_s(measured_at, now),
        "error": row.error if row is not None else None,
        "sourceUrl": row.source_url if row is not None else None,
        "manualBy": row.manual_by if row is not None else None,
        "manualEvidence": row.manual_evidence if row is not None else None,
    }


def _leaves(row: Any | None) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for it in ((row.value or {}).get("items") if row is not None else None) or []:
        items.append({
            "ref": f"{it.get('repo')}#{it.get('number')}",
            "title": it.get("title"),
            "url": it.get("url"),
            "state": leaf_state(it),
            "prs": [{"number": p.get("number"), "url": p.get("url")} for p in it.get("prs") or []],
        })
    counts = {s: sum(1 for i in items if i["state"] == s) for s in LEAF_STATES}
    return {
        "done": counts["done"],
        "inFlight": counts["in_flight"],
        "notStarted": counts["not_started"],
        "total": len(items),
        "measuredAt": iso_utc(row.measured_at) if row is not None else None,
        "items": items,
    }


def build_milestones_view(
    ladder: Any,
    milestones: Sequence[Any],
    measurements: Iterable[Any],
    *,
    now: datetime,
    primary_repo: str | None,
    auto_refresh: bool,
) -> dict[str, Any]:
    """The view model for one ladder. *milestones* are ``Milestone`` rows (any order),
    *measurements* the ladder's ``MilestoneMeasurement`` rows. Milestones with
    ``removed_at`` are dropped unless current."""
    at = aware_utc(now)
    assert at is not None
    rows = list(measurements)
    by_key = {(r.scope, r.key): r for r in rows}
    definition = from_json(ladder.definition) if ladder.definition else None
    def_gate = {m.key: m.gate for m in definition.milestones} if definition else {}

    ordered = sorted(milestones, key=lambda m: (m.ordinal is None, m.ordinal or 0))
    current = next((m for m in ordered if m.state == "current"), None)
    out: list[dict[str, Any]] = []
    for m in ordered:
        if m.removed_at is not None and m is not current:
            continue
        gate_defs = def_gate.get(m.key)
        if gate_defs is None:  # a removed (current) rung keeps its last stored gate
            gate_defs = tuple(condition_from_json(c) for c in m.gate or [])
        gate = [_condition(c, by_key.get((m.key, c.key)), at) for c in gate_defs]
        statuses = [g["status"] for g in gate]
        out.append({
            "key": m.key,
            "label": m.label,
            "name": m.name,
            "audience": m.audience,
            "platforms": [{"name": p.get("name"), "off": bool(p.get("off")), "note": p.get("note")}
                          for p in m.platforms or []],
            "state": m.state,
            "enteredAt": iso_utc(m.entered_at),
            "exitedAt": iso_utc(m.exited_at),
            "statusNote": m.status_note,
            "exitOverride": m.exit_override,
            "epics": [{"ref": r, "url": _ref_url(r)} for r in m.epics or []],
            "gate": gate,
            "gateSummary": {
                "met": statuses.count("met"),
                "total": len(statuses),
                "unknown": statuses.count("unknown"),
                "ready": gate_met(statuses),
            },
            "leaves": _leaves(by_key.get((leaves_scope(m.key), LEAVES_KEY))),
        })

    numbers = [_condition(c, by_key.get(("numbers", c.key)), at)
               for c in (definition.numbers if definition else ())]
    fetched = [t for t in (aware_utc(r.fetched_at) for r in rows) if t is not None]
    spec_path = ladder.spec_path
    return {
        "ladder": {
            "title": ladder.title,
            "specPath": spec_path,
            "specUrl": (f"https://github.com/{primary_repo}/blob/HEAD/{spec_path}"
                        if spec_path and primary_repo else None),
            "sourcePath": ladder.source_path,
            "sourceSha": ladder.source_sha,
            "syncedAt": iso_utc(ladder.synced_at),
            "parseError": ladder.parse_error,
            "parseErrorSha": ladder.parse_error_sha,
            "currentKey": current.key if current is not None else None,
            "currentRemoved": current is not None and current.removed_at is not None,
            "updatedAt": iso_utc(max(fetched)) if fetched else None,
            "autoRefresh": auto_refresh,
        },
        "milestones": out,
        "numbers": numbers,
    }
