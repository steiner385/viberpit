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

from server.pm.milestone_asks import ASK_KIND, DECISION_STATUSES, is_open_ask
from server.pm.milestone_def import (
    MILESTONES_PATH,
    Condition,
    LadderDef,
    condition_from_json,
    from_json,
)
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


def _exit_override(raw: Any) -> dict[str, Any] | None:
    """The stored override as ``{by, reason, at, unmet: [{key, status, value}]}``
    (spec §7), or None when absent or malformed — never a raw blob."""
    if not isinstance(raw, Mapping):
        return None
    by, reason, at = raw.get("by"), raw.get("reason"), raw.get("at")
    if not (isinstance(by, str) and isinstance(reason, str) and isinstance(at, str)):
        return None
    unmet = raw.get("unmet")
    return {
        "by": by, "reason": reason, "at": at,
        "unmet": [{"key": u.get("key"), "status": u.get("status"), "value": u.get("value")}
                  for u in (unmet if isinstance(unmet, list) else []) if isinstance(u, Mapping)],
    }


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
        "manualUrl": row.manual_url if row is not None else None,
    }


def _gate_defs(definition: LadderDef | None, milestone: Any) -> tuple[Condition, ...]:
    """The milestone's gate from the ladder definition; a removed (current) rung,
    gone from the definition, keeps its last stored gate."""
    if definition is not None:
        for m in definition.milestones:
            if m.key == milestone.key:
                return m.gate
    return tuple(condition_from_json(c) for c in milestone.gate or [])


def _gate(gate_defs: Sequence[Condition], milestone_key: str,
          by_key: Mapping[tuple[str, str], Any], now: datetime) -> list[dict[str, Any]]:
    return [_condition(c, by_key.get((milestone_key, c.key)), now) for c in gate_defs]


def read_time_gate(ladder: Any, milestone: Any, measurements: Iterable[Any],
                   now: datetime) -> list[dict[str, Any]]:
    """The milestone's gate conditions with read-time statuses (freshness applied):
    THE gate both the view and ``milestone_actions.advance`` judge. A stale or
    missing reading is ``unknown``, so ``gate_met`` over these is never fooled."""
    at = aware_utc(now)
    assert at is not None
    definition = from_json(ladder.definition) if ladder.definition else None
    by_key = {(r.scope, r.key): r for r in measurements}
    return _gate(_gate_defs(definition, milestone), milestone.key, by_key, at)


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


def _note(n: Any) -> dict[str, Any]:
    return {
        "id": n.id, "kind": n.kind, "ref": n.ref, "text": n.text, "url": n.url,
        "status": n.status, "milestoneKey": n.milestone_key, "createdBy": n.created_by,
        "createdAt": iso_utc(n.created_at), "resolvedBy": n.resolved_by,
        "resolvedAt": iso_utc(n.resolved_at),
    }


def _created(n: Any) -> tuple[bool, datetime | None, str]:
    """Oldest first; the id breaks a tie so the order is total."""
    at = aware_utc(n.created_at)
    return at is None, at, str(n.id)


def _notes(notes: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    """Ladder-wide decisions (locked, proposed, superseded) and asks (open first),
    each group oldest first. Notes on a removed rung stay: the owner resolves them."""
    rank = {s: i for i, s in enumerate(DECISION_STATUSES)}
    rows = list(notes)
    decisions = sorted((n for n in rows if n.kind == "decision"),
                       key=lambda n: (rank.get(n.status, len(rank)), *_created(n)))
    asks = sorted((n for n in rows if n.kind == ASK_KIND),
                  key=lambda n: (not is_open_ask(n), *_created(n)))
    return {"decisions": [_note(n) for n in decisions], "asks": [_note(n) for n in asks]}


def build_milestones_view(
    ladder: Any,
    milestones: Sequence[Any],
    measurements: Iterable[Any],
    *,
    notes: Iterable[Any],
    now: datetime,
    primary_repo: str | None,
    auto_refresh: bool,
) -> dict[str, Any]:
    """The view model for one ladder. *milestones* are ``Milestone`` rows (any order),
    *measurements* the ladder's ``MilestoneMeasurement`` rows, *notes* its
    ``MilestoneNote`` rows. Milestones with ``removed_at`` are dropped unless current."""
    at = aware_utc(now)
    assert at is not None
    rows = list(measurements)
    by_key = {(r.scope, r.key): r for r in rows}
    definition = from_json(ladder.definition) if ladder.definition else None

    ordered = sorted(milestones, key=lambda m: (m.ordinal is None, m.ordinal or 0))
    current = next((m for m in ordered if m.state == "current"), None)
    out: list[dict[str, Any]] = []
    for m in ordered:
        if m.removed_at is not None and m is not current:
            continue
        gate = _gate(_gate_defs(definition, m), m.key, by_key, at)
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
            "exitOverride": _exit_override(m.exit_override),
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
    note_rows = list(notes)
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
            "ladderComplete": current is None and any(m.state == "done" for m in ordered),
            "asksOpen": sum(1 for n in note_rows if is_open_ask(n)),
        },
        "milestones": out,
        "numbers": numbers,
        "notes": _notes(note_rows),
    }


# ---------------------------------------------------------------- text summary

#: Bounds on the session tools' summary (the web renders the view itself).
SUMMARY_MAX = 1500
LABEL_MAX = 60
_GATE_LINES, _ASKS, _DECISIONS, _UPCOMING = 8, 5, 6, 5
_GLYPH = {"met": "✔", "unmet": "✖", "unknown": "?", "info": "·"}
_OP = {"eq": "=", "ne": "≠", "lt": "<", "le": "≤", "gt": ">", "ge": "≥"}


def _clip(text: str | None, limit: int = LABEL_MAX) -> str:
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def _num(x: Any) -> str:
    return str(int(x)) if isinstance(x, float) and x.is_integer() else str(x)


def _reading(c: Mapping[str, Any]) -> str | None:
    """"8 of 9 closed", "293 → 0", "0 of ≥3" or a bare count; None with no number."""
    raw = c.get("value")
    v: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    done, total, n = v.get("done"), v.get("total"), v.get("n")
    if isinstance(done, (int, float)) and isinstance(total, (int, float)):
        return f"{_num(done)} of {_num(total)} closed"
    if not isinstance(n, (int, float)) or isinstance(n, bool):
        return None
    op, target = c.get("op"), c.get("target")
    if op in _OP and target is not None:
        return f"{_num(n)} → {_num(target)}" if op == "eq" else f"{_num(n)} of {_OP[op]}{_num(target)}"
    return _num(n)


def _ago(seconds: Any) -> str | None:
    if not isinstance(seconds, int):
        return None
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def _condition_line(c: Mapping[str, Any]) -> str:
    status = c.get("status") or "unknown"
    if status == "unknown" and c.get("error"):
        detail = f"unknown: {_clip(c['error'], 80)}"
    else:
        reading = _reading(c)
        if reading is None and c.get("manualBy"):
            reading = f"ticked by {c['manualBy']}"
        parts = [p for p in (reading, _ago(c.get("ageSeconds"))) if p]
        if status == "unknown":
            parts.append("stale" if parts else "no reading yet")
        detail = ", ".join(parts)
    line = f"  {_GLYPH.get(status, '?')} {c.get('key')}: {_clip(c.get('label'))}"
    return f"{line} ({detail})" if detail else line


def _listed(items: list[str], limit: int, sep: str) -> str:
    shown = sep.join(items[:limit])
    return f"{shown} +{len(items) - limit} more" if len(items) > limit else shown


def _title(m: Mapping[str, Any]) -> str:
    return " · ".join(p for p in (m.get("label"), m.get("name")) if p) or str(m.get("key"))


def summarize_milestones(view: Mapping[str, Any], *, product: str = "") -> str:
    """THE compact text the session tools put in front of a milestones view: the
    current rung and its gate, leaves, open asks, decisions and what is upcoming.
    Labels are clipped to ``LABEL_MAX``, long lists end "+N more", and the whole is
    at most ``SUMMARY_MAX`` characters. A product with no ladder gets one line."""
    ladder = view.get("ladder")
    if not ladder:
        return f"{product or 'This product'}: no milestone ladder (add {MILESTONES_PATH})"
    milestones = list(view.get("milestones") or [])
    current = next((m for m in milestones if m.get("key") == ladder.get("currentKey")), None)
    title = ladder.get("title") or "Milestones"
    lines: list[str] = []
    if current is None:
        state = "complete" if ladder.get("ladderComplete") else "not started"
        lines.append(f"{title} — ladder {state}")
    else:
        s = current.get("gateSummary") or {}
        head = f"{s.get('met', 0)}/{s.get('total', 0)} met"
        if s.get("unknown"):
            head += f", {s['unknown']} unknown"
        head += f", ready: {'yes' if s.get('ready') else 'no'}"
        removed = " — removed from the definition" if ladder.get("currentRemoved") else ""
        lines.append(f"{title} — current: {_title(current)} ({head}){removed}")
        if current.get("statusNote"):
            lines.append(f"  status: {_clip(current['statusNote'], 120)}")
        gate = [_condition_line(c) for c in current.get("gate") or []]
        lines += gate[:_GATE_LINES]
        if len(gate) > _GATE_LINES:
            lines.append(f"  +{len(gate) - _GATE_LINES} more")
        leaves = current.get("leaves") or {}
        if leaves.get("total"):
            lines.append(f"  leaves: {leaves.get('done', 0)} done · {leaves.get('inFlight', 0)} "
                         f"in flight · {leaves.get('notStarted', 0)} not started")
    if ladder.get("parseError"):
        lines.append(f"Definition error: {_clip(ladder['parseError'], 120)}")
    notes = view.get("notes") or {}
    asks = [a for a in notes.get("asks") or [] if a.get("status") == "open"]
    if asks:
        rows = [(f"[{a['milestoneKey']}] " if a.get("milestoneKey") else "") + _clip(a.get("text"))
                for a in asks]
        lines.append(f"Open asks ({len(rows)}): {_listed(rows, _ASKS, ' · ')}")
    decisions = notes.get("decisions") or []
    if decisions:
        rows = [f"{d.get('ref') or _clip(d.get('text'), 30)} {d.get('status')}" for d in decisions]
        lines.append(f"Decisions ({len(rows)}): {_listed(rows, _DECISIONS, ', ')}")
    upcoming = [_title(m) for m in milestones if m.get("state") == "upcoming"]
    if upcoming:
        lines.append(f"Upcoming: {_listed(upcoming, _UPCOMING, ', ')}")
    text = "\n".join(lines)
    return text if len(text) <= SUMMARY_MAX else text[:SUMMARY_MAX - 1] + "…"
