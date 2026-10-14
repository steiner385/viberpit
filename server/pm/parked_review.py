"""Parked review (design ``docs/superpowers/specs/2026-10-14-needs-you-burden-design.md``
§4): is a ``parked`` issue still worth parking?

A person parks an issue with the ``parked`` label and the queue skips it from then on
(``queue.is_parked``) — so parked issues pile up long after the PR that fixed them
merged, or the code they were about is gone. Once a week (sooner while more than the
product's ``parked_review_threshold`` are parked) a bounded stateless helper reads each
parked issue and the merged PRs that reference it
(``server/automations/parked_reviewer.py``) and answers ONE verdict:

* ``close_fixed`` — a merged PR already implements it (cited);
* ``close_obsolete`` — what it targets is gone or superseded (cited);
* ``unpark`` — still relevant, unblocked and actionable;
* ``keep`` — leave it parked.

Read-only: a non-``keep`` verdict becomes an owner recommendation
(``hygiene.parked_review``), never an applied action whatever the product's
``risk_tolerance`` — a parked label a person placed is only removed, or the issue
closed, when the owner approves.

Pure: no I/O.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from server.pm.closeout_assess import cut_text, json_objects
from server.pm.queue import QueueRow, is_parked

VERDICT_CLOSE_FIXED: Final = "close_fixed"
VERDICT_CLOSE_OBSOLETE: Final = "close_obsolete"
VERDICT_UNPARK: Final = "unpark"
VERDICT_KEEP: Final = "keep"
VERDICTS: tuple[str, ...] = (VERDICT_CLOSE_FIXED, VERDICT_CLOSE_OBSOLETE, VERDICT_UNPARK,
                             VERDICT_KEEP)

#: A product's parked issues are reviewed at least this often…
REVIEW_INTERVAL = timedelta(days=7)
#: …and, while more than its ``parked_review_threshold`` are parked, this often.
THRESHOLD_INTERVAL = timedelta(days=1)
#: Parked issues one review run covers (the least recently reviewed first).
REVIEWS_PER_RUN = 15
#: Helper calls per hygiene tick, fleet-wide: a run spans ticks.
REVIEWS_PER_TICK = 5

#: The issue body and each PR body the prompt quotes, in characters.
_ISSUE_BODY_MAX = 6_000
_PR_BODY_MAX = 1_500
#: Changed paths the prompt lists per PR, and the diff it quotes across all PRs.
_FILES_MAX = 40
_DIFF_MAX = 6_000
#: The evidence a recommendation (and the close comment) carries.
_EVIDENCE_MAX = 1_000

SYSTEM_PROMPT = (
    "You review a GitHub issue a person PARKED (set aside) a while ago, and decide whether "
    "it should stay parked. Answer exactly one verdict: "
    "\"close_fixed\" — one of the merged pull requests listed already implements what the "
    "issue asks; name that PR's number; "
    "\"close_obsolete\" — the issue no longer applies: the PRs show the code or feature it "
    "targets was removed or replaced; cite that evidence; "
    "\"unpark\" — still relevant, nothing visible blocks it, and a coding agent could act "
    "on it now; "
    "\"keep\" — anything else, including when you are unsure. "
    "Never answer close_fixed without a listed merged PR that does the work. Reply with "
    "ONLY a JSON object, no prose and no code fences.")


def run_target(slug: str) -> str:
    """The ``AutomationTargetState`` target of a product's review run."""
    return f"parked-review:{slug}"


def item_target(ref: str) -> str:
    """The ``AutomationTargetState`` target of one parked issue's last review."""
    return f"parked:{ref}"


def ref_of_item_target(target: str) -> str | None:
    return target[len("parked:"):] if target.startswith("parked:") else None


def slug_of_run_target(target: str) -> str | None:
    return target[len("parked-review:"):] if target.startswith("parked-review:") else None


def parked_rows(rows: Iterable[QueueRow]) -> list[QueueRow]:
    """The open issues a person parked (queue rows are open by construction)."""
    return [r for r in rows if is_parked(r.labels)]


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def parse_at(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def run_due(last_run_at: datetime | None, parked: int, threshold: int, *,
            now: datetime) -> bool:
    """Whether a product's review run is due: never run, a week since the last, or —
    with *threshold* above 0 and more than *threshold* parked — a day since."""
    if last_run_at is None:
        return True
    since = now - _aware(last_run_at)
    if since >= REVIEW_INTERVAL:
        return True
    return threshold > 0 and parked > threshold and since >= THRESHOLD_INTERVAL


def pick(rows: Sequence[QueueRow], states: Mapping[str, Mapping], n: int) -> list[QueueRow]:
    """Up to *n* of *rows*, the least recently reviewed first (never reviewed before
    any), then the oldest issue."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)

    def key(r: QueueRow) -> tuple:
        at = parse_at((states.get(r.ref) or {}).get("at"))
        created = _aware(r.gh_created_at) if r.gh_created_at else epoch
        return (at is not None, at or epoch, created, r.ref)

    return sorted(rows, key=key)[:max(0, n)]


def pr_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/pull/{number}"


def build_prompt(title: str, body: str, prs: Sequence[Mapping]) -> str:
    """The user prompt: the parked issue and each merged PR that referenced it
    (title, body, changed files, a diff excerpt — ``_DIFF_MAX`` across all PRs)."""
    lines = [f"Parked issue: {title}", "", "Issue body:", "---",
             cut_text(body or "(empty)", _ISSUE_BODY_MAX), "---"]
    budget = _DIFF_MAX
    for pr in prs:
        files = [str(f) for f in (pr.get("files") or ())]
        lines += ["", f"Merged PR #{pr.get('number')}: {pr.get('title') or ''}",
                  "PR body:", cut_text(str(pr.get("body") or "(empty)"), _PR_BODY_MAX),
                  f"Changed files ({len(files)}): " + ", ".join(files[:_FILES_MAX])
                  + (" …" if len(files) > _FILES_MAX else "")]
        diff = str(pr.get("diff_excerpt") or "")
        if budget > 0 and diff:
            excerpt = diff[:budget]
            budget -= len(excerpt)
            lines += ["Diff excerpt:", "```diff", excerpt, "```"]
    if not prs:
        lines += ["", "No merged pull request references this issue."]
    lines += ["", 'Reply as {"verdict": "close_fixed|close_obsolete|unpark|keep", '
              '"pr": <the merged PR number for close_fixed, else null>, '
              '"evidence": "<why, citing the PR, file or change>", '
              '"summary": "<one sentence>"}.']
    return "\n".join(lines)


@dataclass(frozen=True)
class Verdict:
    verdict: str
    evidence: str = ""
    summary: str = ""
    #: The merged PR a ``close_fixed`` verdict cites.
    pr: int | None = None


def parse_verdict(text: str | None, merged: Iterable[int] = ()) -> Verdict | None:
    """The first JSON object in *text* with a known ``verdict``; ``None`` for anything
    unusable — prose, an unknown verdict, a ``close_fixed`` that cites no PR among
    *merged* (the merged PR numbers the prompt listed), or a close with no evidence."""
    numbers = {int(n) for n in merged}
    for obj in json_objects(text or ""):
        if not isinstance(obj, dict) or "verdict" not in obj:
            continue
        verdict = str(obj.get("verdict") or "").strip().lower()
        if verdict not in VERDICTS:
            return None
        evidence = cut_text(str(obj.get("evidence") or ""), _EVIDENCE_MAX)
        raw_pr = obj.get("pr")
        pr = raw_pr if isinstance(raw_pr, int) and not isinstance(raw_pr, bool) else None
        if verdict == VERDICT_CLOSE_FIXED and pr not in numbers:
            return None
        if verdict in (VERDICT_CLOSE_FIXED, VERDICT_CLOSE_OBSOLETE) and not evidence:
            return None
        return Verdict(verdict, evidence, str(obj.get("summary") or "").strip(),
                       pr if verdict == VERDICT_CLOSE_FIXED else None)
    return None


def review_state(*, fingerprint: str, verdict: Verdict | None, prs: Sequence[Mapping],
                 repo: str, at: str) -> dict:
    """The durable record of one review (target :func:`item_target`): the verdict
    for the issue's body *fingerprint*, the merged PRs it saw (number, title, url —
    the evidence links a recommendation shows), and ``appliedAt`` — stamped by the
    executor once an approved outcome landed, so it is not proposed again. A ``None``
    verdict (an unusable reply) is recorded too: it decides nothing until the next
    review."""
    return {"fingerprint": fingerprint,
            "verdict": verdict.verdict if verdict is not None else None,
            "evidence": verdict.evidence if verdict is not None else "",
            "summary": verdict.summary if verdict is not None else "",
            "pr": verdict.pr if verdict is not None else None,
            "prs": [{"number": int(p.get("number") or 0), "title": str(p.get("title") or ""),
                     "url": pr_url(repo, int(p.get("number") or 0))} for p in prs],
            "at": at, "appliedAt": None}
