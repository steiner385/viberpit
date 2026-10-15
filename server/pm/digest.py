"""The daily owner digest (design ``docs/superpowers/specs/2026-10-14-needs-you-burden-design.md``
§5): every item that truly needs you, grouped by product, each with at most ONE
recommended action, so a morning's worth of "needs you" clears in a few taps.

The population is the needs-you definition (``server/pm/attention.py``, §1) read
from the views that already serve it — the holds read model (``escalation``), the
map's issue tiers and initiative gates (``pm_tree``), pending triage and decision
proposals — plus the pending owner recommendations the hygiene loop left
(``OwnerRecommendation``). ``server/api/pm_digest.py`` loads them; this module only
shapes them and picks the recommendation:

* a model-approval hold → approve the escalation at the requested tier;
* a pending owner recommendation → apply it (close, unpark, release, …);
* a HIGH-confidence triage proposal onto a capability → accept it;
* a pending decision whose recommended key is one of its options → accept that
  option (#3112);
* an initiative gate waiting on a person whose stage carries its completion
  evidence (``sdlc.engine.stage_complete``: the artifact merged, or the build
  epic fully closed) → advance the gate one stage (#3112);
* a SYSTEM-written hand-off on an issue the product's policy now skips
  (``policy_skip.stale_system_holds``, the hygiene pass's own verdict) → release
  the hold (#3112);
* everything else (a checklist, a person's park, a gate with no evidence, a
  decision with no valid recommendation) → none: only a person can choose.

Nothing here acts on its own: a recommendation is applied only by a person's
confirmed tap (``confirm: true``), so a gate is still never advanced without the
human (governance keeps the human as the gate; ``tree.initiative_reason``'s "no
auto-advance" stands). Every applicable action is re-derived from current state
when applied (``POST /api/pm/digest/apply``) — the id names the item, never the
action.

Pure: no I/O.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from server.pm.closeout_assess import cut_text
from server.pm.escalation import (ATTENTION_CHECKLIST, ATTENTION_DECISION,
                                  ATTENTION_EVIDENCE, ATTENTION_MODEL_APPROVAL,
                                  ATTENTION_OWNER_ACTION, HeldItem)
from server.pm.holds import EvidenceRequest

KIND_MODEL_APPROVAL = ATTENTION_MODEL_APPROVAL
KIND_CHECKLIST = ATTENTION_CHECKLIST
KIND_DECISION = ATTENTION_DECISION
KIND_OWNER_ACTION = ATTENTION_OWNER_ACTION
#: An ``owner_evidence`` hold (#3113): proof only the owner can supply (a device
#: check, a legal record). Answered by posting on the issue, not by choosing, so it
#: is listed in its own group (``ownerEvidence``), after everything to decide.
KIND_EVIDENCE = ATTENTION_EVIDENCE
KIND_GATE = "gate"
KIND_TRIAGE = "triage"
KIND_ISSUE = "issue"
KIND_RECOMMENDATION = "recommendation"
KINDS: tuple[str, ...] = (KIND_MODEL_APPROVAL, KIND_RECOMMENDATION, KIND_TRIAGE,
                          KIND_DECISION, KIND_CHECKLIST, KIND_OWNER_ACTION, KIND_GATE,
                          KIND_ISSUE, KIND_EVIDENCE)

#: The recommended actions ``POST /api/pm/digest/apply`` can carry out.
ACTION_APPROVE_ESCALATION = "approve_escalation"
ACTION_APPLY_RECOMMENDATION = "apply_recommendation"
ACTION_ACCEPT_TRIAGE = "accept_triage"
ACTION_ACCEPT_DECISION = "accept_decision"
ACTION_ADVANCE_GATE = "advance_gate"
ACTION_RELEASE_HOLD = "release_hold"
APPLY_ACTIONS: tuple[str, ...] = (ACTION_APPROVE_ESCALATION, ACTION_APPLY_RECOMMENDATION,
                                  ACTION_ACCEPT_TRIAGE, ACTION_ACCEPT_DECISION,
                                  ACTION_ADVANCE_GATE, ACTION_RELEASE_HOLD)

#: Item-id prefixes; ``parse_id`` reads them back.
ID_HOLD = "hold"
ID_REC = "rec"
ID_TRIAGE = "triage"
ID_DECISION = "decision"
ID_GATE = "gate"
ID_ISSUE = "issue"

#: Ids one ``POST /api/pm/digest/apply`` may carry ("approve all" passes every id
#: with a recommendation).
APPLY_BATCH_MAX = 100

#: A product's digest push is sent at most this often — a day, less one hygiene
#: tick, so a send does not drift a tick later every day.
DIGEST_INTERVAL = timedelta(hours=24) - timedelta(minutes=15)
#: The dedupe record: one audit row per digest push sent, per product.
SENT_AUDIT_TYPE = "pm.digest.sent"
#: The audit row of one ``POST /api/pm/digest/apply`` call.
APPLY_AUDIT_TYPE = "pm.digest.apply"

#: The confidence level a triage proposal needs for the digest to recommend
#: accepting it (``classification.CONFIDENCE_LEVELS``' top).
TRIAGE_RECOMMEND_CONFIDENCE = "high"

_WHY_MAX = 300

#: The hygiene actions an owner recommendation carries, as a button label.
_REC_LABEL = {"close": "Close", "set_status": "Move", "release_hold": "Release the hold",
              "prune_edge": "Prune the edge", "rewrite_hold": "Narrow the hold",
              "unpark": "Unpark"}


@dataclass(frozen=True)
class Recommendation:
    action: str
    label: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict:
        return {"action": self.action, "label": self.label, "params": dict(self.params)}


@dataclass(frozen=True)
class DigestItem:
    id: str
    kind: str
    product: str
    title: str
    why: str
    url: str | None
    ref: str | None = None
    #: An initiative gate's slug (``KIND_GATE``).
    slug: str | None = None
    recommendation: Recommendation | None = None
    #: Evidence links (``{"title", "url"}``): the merged PRs a parked review read.
    evidence: tuple[Mapping[str, str], ...] = ()

    def to_wire(self) -> dict:
        return {"id": self.id, "kind": self.kind, "product": self.product, "ref": self.ref,
                "slug": self.slug, "title": self.title, "why": self.why, "url": self.url,
                "recommendation": (self.recommendation.to_wire()
                                   if self.recommendation is not None else None),
                "evidence": [dict(e) for e in self.evidence]}


def item_id(prefix: str, key: str) -> str:
    return f"{prefix}:{key}"


def parse_id(value: str) -> tuple[str, str] | None:
    """``(prefix, key)`` of a digest item id, or ``None`` for a malformed one."""
    prefix, sep, key = str(value).partition(":")
    if not sep or not key or prefix not in (ID_HOLD, ID_REC, ID_TRIAGE, ID_DECISION,
                                             ID_GATE, ID_ISSUE):
        return None
    return prefix, key


def _why(text: str | None) -> str:
    return cut_text(str(text or ""), _WHY_MAX)


def decision_recommendation(proposal_id: str | None, options: Iterable[Any],
                            recommended_key: str | None) -> Recommendation | None:
    """Accept a pending decision's recommended option — only when that key is one of
    its options (a stale or malformed recommendation leaves the choice to a person).
    THE rule the listing and the apply re-derive share."""
    if not proposal_id or not recommended_key:
        return None
    for opt in options or ():
        if isinstance(opt, Mapping) and str(opt.get("key") or "") == recommended_key:
            label = str(opt.get("label") or recommended_key)
            return Recommendation(ACTION_ACCEPT_DECISION, f"Accept: {label}",
                                  {"proposalId": proposal_id, "key": recommended_key})
    return None


#: The held-issue kinds a policy-skipped system hold may be released from: a plain
#: hand-off or a climb the approve tap would refuse anyway. Never a checklist (the
#: close-out assessment owns those) nor a decision (only an accept ends one).
_RELEASABLE_KINDS = (KIND_OWNER_ACTION, KIND_MODEL_APPROVAL)


def from_held(item: HeldItem, *, releasable: bool = False) -> DigestItem:
    """A held issue (the holds read model). *releasable*: the loader found the hold
    among the SYSTEM-written hand-offs the product's policy now skips
    (``policy_skip.stale_system_holds``) — release it; that wins over approving a
    climb, which the approve tap refuses for a skipped issue. Otherwise a model
    approval recommends approving the climb it asked for, a decision with a valid
    recommended option recommends accepting it, and a checklist or a hand-off
    recommends nothing — only a person can do it."""
    rec = None
    if (releasable and item.work_item_id and item.hold_kind == "owner_action"
            and item.attention_kind in _RELEASABLE_KINDS):
        rec = Recommendation(ACTION_RELEASE_HOLD, _REC_LABEL["release_hold"],
                             {"workItemId": item.work_item_id})
    elif item.escalation is not None and item.work_item_id:
        tier = item.escalation.needs_approval
        rec = Recommendation(ACTION_APPROVE_ESCALATION, f"Approve {tier}",
                             {"workItemId": item.work_item_id, "model": tier})
    elif item.attention_kind == KIND_DECISION:
        rec = decision_recommendation(item.proposal_id, item.options, item.recommended_key)
    why = item.hold_reason
    if item.attention_kind == KIND_DECISION:
        why = item.hold_reason or "A decision is waiting for you to pick an option."
    elif item.attention_kind == KIND_EVIDENCE:
        why = evidence_why(item.evidence, item.hold_reason)
    return DigestItem(id=item_id(ID_HOLD, item.work_item_id or item.ref),
                      kind=item.attention_kind, product=item.product,
                      title=item.title or item.ref, why=_why(why), url=item.url or None,
                      ref=item.ref, recommendation=rec)


#: An ``owner_evidence`` hold with no readable request (the writer always gives one;
#: this is the read-side fallback).
WHY_EVIDENCE = "Evidence only you can supply is waiting — post it on the issue."


def evidence_why(evidence: Mapping | None, hold_reason: str | None) -> str:
    """What an ``owner_evidence`` ask wants, and where to give it (#3113)."""
    req = EvidenceRequest.from_wire(evidence, fallback_request=hold_reason or "")
    if not req.request:
        return WHY_EVIDENCE
    return f"{req.request.rstrip('.')}. Post it as {req.where}."


def from_decision(*, proposal_id: str, product: str, ref: str | None, title: str | None,
                  url: str | None, rationale: str | None, options: Iterable[Any] = (),
                  recommended_key: str | None = None) -> DigestItem:
    """A pending decision proposal not already listed as a held issue: accept its
    recommended option (``decision_recommendation``)."""
    return DigestItem(id=item_id(ID_DECISION, proposal_id), kind=KIND_DECISION,
                      product=product, title=title or ref or "Decision",
                      why=_why(rationale or "A decision is waiting for you to pick an option."),
                      url=url, ref=ref,
                      recommendation=decision_recommendation(proposal_id, options,
                                                             recommended_key))


def from_triage(*, proposal_id: str, product: str, ref: str | None, title: str | None,
                url: str | None, feature_slug: str | None,
                confidence_level: str | None) -> DigestItem:
    """A triage proposal awaiting a decision: accept it when the classifier is
    confident AND it names a capability (a no-fit needs a person)."""
    rec = None
    if confidence_level == TRIAGE_RECOMMEND_CONFIDENCE and feature_slug:
        rec = Recommendation(ACTION_ACCEPT_TRIAGE, f"Accept into {feature_slug}",
                             {"proposalId": proposal_id, "featureSlug": feature_slug})
    home = f"proposed for {feature_slug}" if feature_slug else "no capability fits"
    return DigestItem(id=item_id(ID_TRIAGE, proposal_id), kind=KIND_TRIAGE, product=product,
                      title=title or ref or "Triage proposal",
                      why=f"Awaiting triage — {home} ({confidence_level or 'unknown'} confidence)",
                      url=url, ref=ref, recommendation=rec)


#: An initiative gate's "why", by its reason / status.
_GATE_WHY = {"advance": "Its build epic is done; the stage gate is waiting for you.",
             "awaiting_human": "Waiting for your review at its stage gate."}


def gate_advance_recommended(*, status: str | None, reason: str | None,
                             stage_complete: bool) -> bool:
    """Whether the digest recommends advancing an initiative's gate: the GATE is
    what waits on a person (``awaiting_human``, or ``tree.initiative_reason``'s
    ``advance`` — the build epic done, the gate not moved) AND its stage carries the
    completion evidence (``sdlc.engine.stage_complete``). THE rule the listing and
    the apply re-derive share.

    The action is the one-stage advance (``POST /api/sdlc/initiatives/{slug}/advance``),
    never ``close``: ``stage_complete`` is exactly the evidence an advance names,
    while ``close`` jumps to the terminal stage and needs proof the item does not
    carry (the epic ISSUE itself closed). An initiative that needs you only for
    its epic's held issues is not a gate ask — those issues are listed themselves."""
    return stage_complete and (status == "awaiting_human" or reason == "advance")


def from_initiative(product: str, node: Mapping, *, stage_complete: bool = False) -> DigestItem:
    """An initiative whose OWN tier needs you: a gate only a person moves. When the
    gate waits on its stage's completed evidence (``gate_advance_recommended``;
    *stage_complete* is the loader's ``sdlc.engine.stage_complete``), recommend the
    advance — applied only by a person's confirmed tap. Otherwise nothing; the item
    links to the gate."""
    slug = str(node.get("slug") or "")
    held = int((node.get("epic") or {}).get("held") or 0)
    why = (_GATE_WHY.get(str(node.get("reason") or ""))
           or _GATE_WHY.get(str(node.get("status") or ""))
           or (f"{held} issue(s) in its epic are held for you." if held else
               "Waiting for you at its stage gate."))
    rec = None
    if slug and gate_advance_recommended(status=node.get("status"), reason=node.get("reason"),
                                         stage_complete=stage_complete):
        rec = Recommendation(ACTION_ADVANCE_GATE, "Advance the stage gate", {"slug": slug})
    return DigestItem(id=item_id(ID_GATE, slug), kind=KIND_GATE, product=product,
                      title=str(node.get("title") or slug), why=why,
                      url=f"/initiatives/{slug}", slug=slug, recommendation=rec)


#: A needs-you issue's "why" when it carries no hold (``tree.issue_tier``).
WHY_NEEDS_SPEC = "Bounced out of the runner — it needs a better spec."
WHY_SESSION_WAIT = "Its session is waiting for you."
WHY_ASSIGNED = "Assigned to you on GitHub."


def from_issue(*, product: str, work_item_id: str, ref: str, title: str | None,
               url: str | None, why: str) -> DigestItem:
    """A needs-you issue the holds read model does not list (needs a spec, a
    session waiting on a person, assigned to the owner)."""
    return DigestItem(id=item_id(ID_ISSUE, work_item_id), kind=KIND_ISSUE, product=product,
                      title=title or ref, why=why, url=url, ref=ref)


def from_recommendation(rec: Mapping, *, url: str | None = None) -> DigestItem:
    """A pending owner recommendation (``pm_hygiene.recommendation_to_wire`` shape): its own
    action, with any evidence links it carries (a parked review's merged PRs)."""
    detail = rec.get("detail") or {}
    action = str(rec.get("action") or "")
    label = _REC_LABEL.get(action, action.replace("_", " ").capitalize())
    if action == "set_status" and detail.get("status"):
        label = f"Move to {detail['status']}"
    if action == "close" and detail.get("closeReason") == "not_planned":
        label = "Close as not planned"
    if action == "close" and rec.get("rule") == EVIDENCE_POSTED_RULE:
        label = "Close — evidence posted"
    evidence = tuple({"title": f"#{p.get('number')} {p.get('title') or ''}".strip(),
                      "url": str(p.get("url"))}
                     for p in (detail.get("prs") or ()) if isinstance(p, Mapping) and p.get("url"))
    ref = rec.get("ref")
    title = detail.get("title") or ref or "Recommendation"
    return DigestItem(id=item_id(ID_REC, str(rec.get("id"))), kind=KIND_RECOMMENDATION,
                      product=str(rec.get("product") or ""), title=str(title),
                      why=_why(rec.get("evidence")), url=url, ref=ref,
                      recommendation=Recommendation(ACTION_APPLY_RECOMMENDATION, label,
                                                    {"recommendationId": rec.get("id"),
                                                     "rule": rec.get("rule"),
                                                     "hygieneAction": action}),
                      evidence=evidence)


#: The needs-you hygiene rule whose close follows an owner's evidence reply
#: (``hygiene.RULE_EVIDENCE_POSTED``; spelled here to keep this module pure of it).
EVIDENCE_POSTED_RULE = "evidence_posted"


def _sort_key(item: DigestItem) -> tuple:
    return (item.recommendation is None, KINDS.index(item.kind) if item.kind in KINDS
            else len(KINDS), item.ref or item.slug or "", item.id)


def assemble(items: Iterable[DigestItem], *, products: Iterable[str],
             now: datetime) -> dict:
    """The wire digest: ``{generatedAt, products: [{slug, items, ownerEvidence}],
    total, withRecommendation}``. An ``owner_evidence`` ask with no recommendation
    goes in its product's ``ownerEvidence`` group, apart from what needs deciding
    (#3113); ``total`` counts both groups. One item per thing: when an owner recommendation targets
    an issue the digest already lists (a held issue, a needs-you issue), the
    recommendation stands in for it — even when the item carries a recommendation
    of its own (a hygiene pass that left a ``release_hold`` recommendation and the
    held item's own release are one action, not two). Within a product, items with
    a recommendation come first."""
    items = list(items)
    rec_refs = {i.ref for i in items if i.kind == KIND_RECOMMENDATION and i.ref}
    kept = [i for i in items if i.kind == KIND_RECOMMENDATION or not i.ref
            or i.ref not in rec_refs]
    by_product: dict[str, list[DigestItem]] = {slug: [] for slug in products}
    for i in kept:
        by_product.setdefault(i.product, []).append(i)
    out: list[dict[str, Any]] = [
        {"slug": slug,
         "items": [i.to_wire() for i in sorted(group, key=_sort_key) if not _is_ask(i)],
         "ownerEvidence": [i.to_wire() for i in sorted(group, key=_sort_key) if _is_ask(i)]}
        for slug, group in sorted(by_product.items()) if group]
    total = sum(len(p["items"]) + len(p["ownerEvidence"]) for p in out)
    with_rec = sum(1 for p in out for i in p["items"] if i["recommendation"])
    return {"generatedAt": now.isoformat(), "products": out, "total": total,
            "withRecommendation": with_rec}


def _is_ask(item: DigestItem) -> bool:
    """An ``owner_evidence`` ask still waiting on the owner's reply (no close
    recommendation yet) — the ``ownerEvidence`` group's members."""
    return item.kind == KIND_EVIDENCE and item.recommendation is None


def digest_due(last_sent: datetime | None, now: datetime) -> bool:
    """Whether a product's digest push is due (``DIGEST_INTERVAL``)."""
    if last_sent is None:
        return True
    last = last_sent.replace(tzinfo=timezone.utc) if last_sent.tzinfo is None else last_sent
    return now - last >= DIGEST_INTERVAL


def summary_text(slug: str, total: int, with_rec: int) -> tuple[str, str]:
    """The push's title and its one summary line."""
    noun = "item needs" if total == 1 else "items need"
    have = "has" if with_rec == 1 else "have"
    return (f"{slug}: today's digest",
            f"{total} {noun} you — {with_rec} {have} a recommendation")


def digest_path(slug: str) -> str:
    """Where the push opens: the product's Needs-you tab, digest on top."""
    return f"/roadmap/{slug}?tab=needs-you"
