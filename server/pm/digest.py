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
* a decision whose proposer named a recommended option → accept that option
  (#3112; the person still takes the decision — by approving the recommendation);
* a system-written hand-off the queue skips anyway (``policy_skip.stale_system_holds``,
  the hygiene loop's own rule 3) → release it (#3112) — ahead of the approve
  button, which would re-dispatch an issue the product's policy no longer treats
  as work;
* an initiative gate whose build epic is done (``tree.initiative_reason`` says
  ``advance``) → advance it (#3112). The human is still the gate: nothing moves
  until they approve the recommendation, and ``awaiting_human`` rows (a plan or
  design waiting for review) carry none;
* everything else (a checklist, a hand-off that is still work, an
  ``owner_evidence`` ask — #3113, its own group) → none: only a person can
  choose, do or supply it.

Every applicable action is re-derived from current state when applied
(``POST /api/pm/digest/apply``) — the id names the item, never the action.

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
from server.sdlc.engine import advance_stage

KIND_MODEL_APPROVAL = ATTENTION_MODEL_APPROVAL
KIND_CHECKLIST = ATTENTION_CHECKLIST
KIND_DECISION = ATTENTION_DECISION
#: An ``owner_evidence`` hold (#3113): the owner supplies a fact no agent can (a
#: device check, a legal record). Its own group, after everything a person
#: DECIDES — these are answered by posting on the issue, not by choosing.
KIND_EVIDENCE = ATTENTION_EVIDENCE
KIND_OWNER_ACTION = ATTENTION_OWNER_ACTION
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
#: #3112: accept the option the proposer recommended (``decisions.accept``).
ACTION_ACCEPT_DECISION = "accept_decision"
#: #3112: release a system hand-off the queue skips anyway
#: (``holds.release_policy_skipped_holds``, signal ``policy_skip``).
ACTION_RELEASE_HOLD = "release_hold"
#: #3112: advance an initiative whose build epic is done (the advance route's own
#: sequence, ``server/api/sdlc.py:advance_initiative_row``).
ACTION_ADVANCE_INITIATIVE = "advance_initiative"
APPLY_ACTIONS: tuple[str, ...] = (ACTION_APPROVE_ESCALATION, ACTION_APPLY_RECOMMENDATION,
                                  ACTION_ACCEPT_TRIAGE, ACTION_ACCEPT_DECISION,
                                  ACTION_RELEASE_HOLD, ACTION_ADVANCE_INITIATIVE)

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


def option_label(options: Iterable[Any], key: str | None) -> str | None:
    """The label of the option *key* names among a proposal's *options* (the
    ``{"key", "label", "detail"}`` rows ``decisions.normalize_options`` writes), or
    ``None`` when no option carries it — a recommendation must name a choice the
    person can actually see."""
    for opt in options or ():
        if isinstance(opt, Mapping) and str(opt.get("key") or "") == key:
            return str(opt.get("label") or key)
    return None


def decision_recommendation(*, proposal_id: str | None, recommended_key: str | None,
                            options: Iterable[Any]) -> Recommendation | None:
    """#3112: accept the option the proposer recommended, or ``None`` when the
    proposal names no (visible) recommended option. ONE builder for a decision
    that reaches the digest as a held issue and for one that reaches it on its
    own (``from_decision``)."""
    if not proposal_id or not recommended_key:
        return None
    label = option_label(options, recommended_key)
    if label is None:
        return None
    return Recommendation(ACTION_ACCEPT_DECISION, f"Accept {label}",
                          {"proposalId": proposal_id, "chosenKey": recommended_key})


def from_held(item: HeldItem, *, releasable: bool = False) -> DigestItem:
    """A held issue (the holds read model): a model approval recommends approving
    the climb it asked for; a decision with a recommended option recommends
    accepting it (#3112); a hand-off the queue skips anyway (*releasable*:
    ``policy_skip.stale_system_holds`` named it) recommends releasing it — ahead
    of any approve button, which would re-dispatch non-work (#3112); a checklist,
    an owner-evidence ask or any other hand-off recommends nothing — only a person
    can do, choose or supply it."""
    rec = None
    if releasable and item.work_item_id:
        rec = Recommendation(ACTION_RELEASE_HOLD, "Release the hold",
                             {"workItemId": item.work_item_id})
    elif item.escalation is not None and item.work_item_id:
        tier = item.escalation.needs_approval
        rec = Recommendation(ACTION_APPROVE_ESCALATION, f"Approve {tier}",
                             {"workItemId": item.work_item_id, "model": tier})
    elif item.attention_kind == KIND_DECISION:
        rec = decision_recommendation(proposal_id=item.proposal_id,
                                      recommended_key=item.recommended_key,
                                      options=item.options)
    why = item.hold_reason
    if item.attention_kind == KIND_DECISION:
        why = item.hold_reason or "A decision is waiting for you to pick an option."
    elif item.attention_kind == KIND_EVIDENCE:
        why = item.hold_reason or WHY_EVIDENCE
    elif releasable:
        why = (f"{item.hold_reason} — but the queue skips this issue anyway, so "
               "nothing waits on you." if item.hold_reason else
               "The queue skips this issue anyway, so nothing waits on you.")
    return DigestItem(id=item_id(ID_HOLD, item.work_item_id or item.ref),
                      kind=item.attention_kind, product=item.product,
                      title=item.title or item.ref, why=_why(why), url=item.url or None,
                      ref=item.ref, recommendation=rec)


def from_decision(*, proposal_id: str, product: str, ref: str | None, title: str | None,
                  url: str | None, rationale: str | None, recommended_key: str | None = None,
                  options: Iterable[Any] = ()) -> DigestItem:
    """A pending decision proposal not already listed as a held issue; recommends
    the proposer's recommended option when it names one (#3112)."""
    return DigestItem(id=item_id(ID_DECISION, proposal_id), kind=KIND_DECISION,
                      product=product, title=title or ref or "Decision",
                      why=_why(rationale or "A decision is waiting for you to pick an option."),
                      url=url, ref=ref,
                      recommendation=decision_recommendation(
                          proposal_id=proposal_id, recommended_key=recommended_key,
                          options=options))


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

#: The one initiative reason the digest recommends acting on (``tree.initiative_reason``).
GATE_REASON_ADVANCE = "advance"


def from_initiative(product: str, node: Mapping) -> DigestItem:
    """An initiative whose OWN tier needs you: a gate only a person moves. One
    whose build epic is done (reason ``advance``) recommends advancing it to the
    next stage (#3112) — the person is still the gate, the recommendation is what
    they approve; an ``awaiting_human`` row (a plan or design to review) carries
    none and links to the gate."""
    slug = str(node.get("slug") or "")
    held = int((node.get("epic") or {}).get("held") or 0)
    reason = str(node.get("reason") or "")
    why = (_GATE_WHY.get(reason)
           or _GATE_WHY.get(str(node.get("status") or ""))
           or (f"{held} issue(s) in its epic are held for you." if held else
               "Waiting for you at its stage gate."))
    rec = None
    if reason == GATE_REASON_ADVANCE and slug:
        try:
            nxt = advance_stage(str(node.get("stage") or ""))
        except ValueError:
            nxt = None
        if nxt is not None:
            rec = Recommendation(ACTION_ADVANCE_INITIATIVE, f"Advance to {nxt}",
                                 {"slug": slug, "toStage": nxt})
    return DigestItem(id=item_id(ID_GATE, slug), kind=KIND_GATE, product=product,
                      title=str(node.get("title") or slug), why=why,
                      url=f"/initiatives/{slug}", slug=slug, recommendation=rec)


#: An ``owner_evidence`` hold whose reason is blank (the writer always gives one;
#: this is the read-side fallback).
WHY_EVIDENCE = "Evidence only you can supply is waiting — post it on the issue."

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
    if action == "close" and rec.get("rule") == "evidence_posted":
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


def _sort_key(item: DigestItem) -> tuple:
    return (item.recommendation is None, KINDS.index(item.kind) if item.kind in KINDS
            else len(KINDS), item.ref or item.slug or "", item.id)


def assemble(items: Iterable[DigestItem], *, products: Iterable[str],
             now: datetime) -> dict:
    """The wire digest: ``{generatedAt, products: [{slug, items}], total,
    withRecommendation}``. One item per thing: when an owner recommendation targets
    an issue the digest already lists without a recommendation (a held issue, a
    needs-you issue), the recommendation stands in for it. Within a product, items
    with a recommendation come first."""
    items = list(items)
    rec_refs = {i.ref for i in items if i.kind == KIND_RECOMMENDATION and i.ref}
    kept = [i for i in items if i.kind == KIND_RECOMMENDATION or i.recommendation is not None
            or not i.ref or i.ref not in rec_refs]
    by_product: dict[str, list[DigestItem]] = {slug: [] for slug in products}
    for i in kept:
        by_product.setdefault(i.product, []).append(i)
    out: list[dict[str, Any]] = [
        {"slug": slug, "items": [i.to_wire() for i in sorted(group, key=_sort_key)]}
           for slug, group in sorted(by_product.items()) if group]
    total = sum(len(p["items"]) for p in out)
    with_rec = sum(1 for p in out for i in p["items"] if i["recommendation"])
    return {"generatedAt": now.isoformat(), "products": out, "total": total,
            "withRecommendation": with_rec}


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
