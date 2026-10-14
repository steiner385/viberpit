"""Close-out assessment (design ``docs/superpowers/specs/2026-10-14-needs-you-burden-design.md``
§3): what a "merged PR left the issue open" hold really leaves for a person.

The Backlog Runner's close-out (``backlog_github._human_gate_reason``) keeps an issue
open — and hands it to a person — when the merged PR referenced it with a
non-closing keyword or unchecked acceptance items remain. Most of those are not a
person's work: the PR already did the item, or an agent could. A bounded stateless
helper call (``server/automations/closeout_assessor.py``) classifies each remaining
item, and :func:`decide` turns that into ONE outcome the needs-you hygiene executor
applies (risk-gated, dry-run-able, audited):

* every item ``done`` → close the issue, the comment citing the evidence;
* nothing ``human_only`` but something ``agent_doable`` → release the hold so the
  runner continues;
* anything ``human_only`` → rewrite the hold reason to the checklist of exactly those
  items (``CHECKLIST_PREFIX``) and keep the hold.

Pure: no I/O.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from server.automations.backlog_github import (CLOSEOUT_HOLD_PREFIX,
                                               LEGACY_CLOSEOUT_HOLD_REASON)

VERDICT_DONE = "done"
VERDICT_AGENT = "agent_doable"
VERDICT_HUMAN = "human_only"
VERDICTS: tuple[str, ...] = (VERDICT_DONE, VERDICT_AGENT, VERDICT_HUMAN)

DECISION_CLOSE: Final = "close"
DECISION_RELEASE: Final = "release"
DECISION_CHECKLIST: Final = "checklist"
DecisionKind = Literal["close", "release", "checklist"]

#: The head of a hold reason the assessment rewrote: what is left is a person's.
#: The holds read model (``escalation.attention_kind``) keys "checklist" on it.
CHECKLIST_PREFIX = "Left for you:"
#: A hold reason is a one-card read; the checklist stays within this.
CHECKLIST_MAX_CHARS = 500
#: Items the checklist names before "(+N more)".
CHECKLIST_SHOWN = 5
#: One checklist line is cut to this many characters.
_ITEM_MAX_CHARS = 120
#: The PR diff the prompt quotes, across every PR, in characters.
DIFF_EXCERPT_MAX = 12_000
#: The PR body the prompt quotes, per PR, and the issue body, in characters.
_PR_BODY_MAX = 2_000
_ISSUE_BODY_MAX = 6_000
#: Changed paths the prompt lists, per PR.
_FILES_MAX = 60
#: The close comment's cap, so a verbose model cannot post an essay.
_COMMENT_MAX = 3_000

#: When the issue has no unchecked items, the one thing to classify.
REST_OF_ISSUE = "the rest of the issue not covered by the merged PR(s)"

SYSTEM_PROMPT = (
    "You review a GitHub issue that a merged pull request referenced without closing it. "
    "Classify each remaining item as exactly one of: "
    "\"done\" — the merged PR(s) already do it; cite the evidence (a file and line or hunk "
    "from the diff); "
    "\"agent_doable\" — code, test or documentation work a coding agent could still do in "
    "the repository; "
    "\"human_only\" — needs a person: a production or hardware check, credentials or an "
    "external account, a product judgement, or anything outside the repository. "
    "Never call an item done without evidence in the diff. Reply with ONLY a JSON object, "
    "no prose and no code fences.")


def is_closeout_hold(hold_kind: str | None, hold_reason: str | None) -> bool:
    """Whether a hold is the runner's close-out hand-off (``backlog_github
    .closeout_hold_reason``, either spelling)."""
    if hold_kind != "owner_action" or not hold_reason:
        return False
    reason = hold_reason.strip()
    return reason.startswith(CLOSEOUT_HOLD_PREFIX) or reason == LEGACY_CLOSEOUT_HOLD_REASON


def is_checklist_reason(hold_reason: str | None) -> bool:
    """Whether a hold reason is an assessment's checklist (:func:`checklist_reason`)."""
    return bool(hold_reason) and str(hold_reason).lstrip().startswith(CHECKLIST_PREFIX)


def _cut(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def build_prompt(issue_title: str, issue_body: str, unchecked: Sequence[str],
                 prs: Sequence[Mapping]) -> str:
    """The user prompt: the issue, the items to classify (its unchecked acceptance
    items, else :data:`REST_OF_ISSUE`), and each merged PR's title, body, changed
    files and a diff excerpt — ``DIFF_EXCERPT_MAX`` characters across all PRs."""
    items = list(unchecked) or [REST_OF_ISSUE]
    lines = [f"Issue: {issue_title}", "", "Issue body:", "---",
             _cut(issue_body or "(empty)", _ISSUE_BODY_MAX), "---", "",
             "Items to classify:"]
    lines += [f"{i}. {item}" for i, item in enumerate(items, 1)]
    budget = DIFF_EXCERPT_MAX
    for pr in prs:
        files = [str(f) for f in (pr.get("files") or ())]
        lines += ["", f"Merged PR #{pr.get('number')}: {pr.get('title') or ''}",
                  "PR body:", _cut(str(pr.get("body") or "(empty)"), _PR_BODY_MAX),
                  f"Changed files ({len(files)}): " + ", ".join(files[:_FILES_MAX])
                  + (" …" if len(files) > _FILES_MAX else "")]
        diff = str(pr.get("diff_excerpt") or "")
        if budget > 0 and diff:
            excerpt = diff[:budget]
            budget -= len(excerpt)
            lines += ["Diff excerpt:", "```diff", excerpt, "```"]
    if not prs:
        lines += ["", "No merged PR could be read; classify from the issue alone."]
    lines += ["", 'Reply as {"items": [{"text": "<the item>", "verdict": '
              '"done|agent_doable|human_only", "evidence": "<file:line or why>"}], '
              '"summary": "<one sentence>"} — one entry per item, in order.']
    return "\n".join(lines)


@dataclass(frozen=True)
class AssessedItem:
    text: str
    verdict: str
    evidence: str = ""


@dataclass(frozen=True)
class Assessment:
    items: tuple[AssessedItem, ...]
    summary: str = ""


def _json_objects(text: str):
    dec = json.JSONDecoder()
    idx = 0
    while True:
        start = text.find("{", idx)
        if start == -1:
            return
        try:
            obj, _end = dec.raw_decode(text, start)
        except ValueError:
            idx = start + 1
            continue
        yield obj
        idx = start + 1


def parse_verdict(text: str | None) -> Assessment | None:
    """The first JSON object in *text* with a non-empty ``items`` list whose every
    entry has text and a known verdict; ``None`` for anything else (prose, an
    unknown verdict, no items) — an unusable reply decides nothing."""
    for obj in _json_objects(text or ""):
        if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
            continue
        items: list[AssessedItem] = []
        for raw in obj["items"]:
            if not isinstance(raw, dict):
                return None
            item_text = str(raw.get("text") or "").strip()
            verdict = str(raw.get("verdict") or "").strip().lower()
            if not item_text or verdict not in VERDICTS:
                return None
            items.append(AssessedItem(item_text, verdict, str(raw.get("evidence") or "").strip()))
        if not items:
            return None
        return Assessment(tuple(items), str(obj.get("summary") or "").strip())
    return None


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    #: The close comment, the release note, or the checklist hold reason.
    text: str


def checklist_reason(items: Sequence[str]) -> str:
    """``"Left for you:\\n- [ ] <item>…"`` — at most ``CHECKLIST_SHOWN`` items and
    ``CHECKLIST_MAX_CHARS`` characters, the rest counted as ``(+N more)``."""
    cut = [_cut(i, _ITEM_MAX_CHARS) for i in items]
    shown = cut[:CHECKLIST_SHOWN]
    while True:
        more = len(cut) - len(shown)
        text = "\n".join([CHECKLIST_PREFIX, *(f"- [ ] {i}" for i in shown)]
                         + ([f"(+{more} more)"] if more else []))
        if len(text) <= CHECKLIST_MAX_CHARS or len(shown) <= 1:
            return text[:CHECKLIST_MAX_CHARS]
        shown = shown[:-1]


def _close_comment(items: Sequence[AssessedItem]) -> str:
    lines = ["The merged PR(s) already cover what this issue still listed:"]
    lines += [f"- {_cut(i.text, _ITEM_MAX_CHARS)} — {_cut(i.evidence or 'see the PR', 300)}"
              for i in items]
    return _cut("\n".join(lines), _COMMENT_MAX)


def assessment_state(*, fingerprint: str, decision: Decision | None, summary: str,
                     at: str) -> dict:
    """The durable record of one assessment (``AutomationTargetState`` of the
    needs-you hygiene automation, target = the issue ref): one per (issue, body
    fingerprint), so a re-hold of an unchanged issue is not re-assessed. A ``None``
    decision (an unusable reply) is recorded too — retried only once the body
    changes. ``appliedAt`` is stamped by the executor once the outcome landed; the
    rule stops proposing it from then on."""
    return {"fingerprint": fingerprint,
            "decision": decision.kind if decision is not None else None,
            "text": decision.text if decision is not None else "",
            "summary": summary, "at": at, "appliedAt": None}


def decide(assessment: Assessment | None) -> Decision | None:
    """THE outcome rule. ``None`` (an unusable reply) decides nothing."""
    if assessment is None or not assessment.items:
        return None
    human = [i.text for i in assessment.items if i.verdict == VERDICT_HUMAN]
    if human:
        return Decision(DECISION_CHECKLIST, checklist_reason(human))
    agent = [i.text for i in assessment.items if i.verdict == VERDICT_AGENT]
    if agent:
        note = "Left for an agent: " + "; ".join(_cut(t, _ITEM_MAX_CHARS) for t in agent)
        return Decision(DECISION_RELEASE, _cut(note, CHECKLIST_MAX_CHARS))
    return Decision(DECISION_CLOSE, _close_comment(assessment.items))
