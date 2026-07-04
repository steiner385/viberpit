<!-- docs/superpowers/specs/2026-07-03-autopilot-completion-bar-design.md -->
# Autopilot completion bar — hardened "done" + retro archive audit

## Problem

Autopilot has been archiving sessions prematurely. The reported case: a "cicd
doctor" session wrote a spec, emitted its own completion marker
(`<!-- ccswitch:complete -->`), reached status `complete`, and autopilot archived
it — even though no code was written and no PR was opened.

### Root cause

Completion is two-stage:

1. The **session** emits the completion marker → `derive_status` yields status
   `complete` (`server/sessions/status.py`).
2. Autopilot's LLM **decider** (`server/automations/autopilot_decider.py`) re-confirms
   and, in `auto` mode, archives via `AutopilotLoop._advance_one`'s `complete` branch
   (`server/automations/autopilot.py`).

The decider's guidance (`autopilot_guidance.md`) already says a code task is only
complete with "an open PR with auto-merge armed." But the `DecisionContext`
(`server/automations/autopilot_policy.py`) handed to the decider contains **no
ground-truth about PR or git state** — only `status`, `effective_goal`,
`goal_source`, `stuck`, and a 12-entry `transcript_tail`. So the decider *infers*
completion from transcript prose. "I finished the spec" reads as goal-met, and the
session is archived.

The fix is to **feed the decider verified PR facts** (and add a deterministic
backstop), not to reword the guidance again.

## Goals

1. **Raise the bar for "done."** A code task may only be archived by autopilot when a
   real, authored PR is **armed** (open, non-draft, auto-merge enabled) — verified,
   not inferred. A genuinely non-code goal (research / diagnosis / Q&A) may still
   complete without a PR (the "research escape"), judged from the effective goal.
2. **Retroactively audit** already-archived sessions against the same bar; **report**
   all findings and **auto-unarchive** the confidently-premature ones.

## Non-goals

- Changing how a session *emits* the completion marker, or `derive_status`.
- Enabling/arming auto-merge on the session's behalf (autopilot may *nudge* the
  session to do it; it does not run `gh pr merge --auto` itself).
- Any change to the backlog-runner's dedicated auto-archive path
  (`_maybe_autoarchive_completed`) — autopilot already excludes backlog-runner
  `complete` sessions from governance.

## Design overview

Three units, one shared definition of "done":

```
                        ┌─────────────────────────────┐
                        │ completion_bar.met_bar(...)  │  ← single source of truth
                        │ (pure: goal + pr_facts →     │
                        │  met? + reason + remedy)     │
                        └───────────▲─────────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              │ (live)              │             (retro)  │
   ┌──────────┴──────────┐         │        ┌──────────────┴───────────┐
   │ autopilot get_context│         │        │ archive_audit            │
   │ + guidance + gate    │         │        │ (offline gh + unarchive) │
   └──────────────────────┘         │        └──────────────────────────┘
```

### Unit 1 — `server/automations/completion_bar.py` (the bar, pure)

The single source of truth for "is this session's goal met?". No I/O. Both the live
autopilot path and the retro audit call it, satisfying constitution VI (no parallel
implementations of "done").

**Data:**

```python
@dataclass(frozen=True)
class PrFact:
    number: int
    state: str            # 'open' | 'draft' | 'merged' | 'closed'
    is_draft: bool
    auto_merge_enabled: bool
    url: str

@dataclass(frozen=True)
class PrFacts:
    has_repo: bool              # session is attached to a repo (code-oriented)
    prs: tuple[PrFact, ...]     # authored PRs discovered (live: poller cache; retro: gh)

    @property
    def armed_pr(self) -> PrFact | None:   # open, not draft, auto-merge on
        ...
    @property
    def merged_pr(self) -> PrFact | None:  # code landed
        ...
    @property
    def open_unarmed_pr(self) -> PrFact | None:  # open but draft or auto-merge off
        ...

@dataclass(frozen=True)
class BarVerdict:
    met: bool
    reason: str
    remedy: str | None        # nudge text when not met (e.g. "ready + arm the PR")
    confidence: str           # 'high' | 'low'  — retro auto-unarchive only on high
```

**Rule (`met_bar(goal, goal_source, facts) -> BarVerdict`):**

| Condition | Verdict |
| --- | --- |
| `facts.merged_pr` is not None | met — code landed (high) |
| `facts.armed_pr` is not None | met — armed PR (high) |
| `facts.open_unarmed_pr` is not None | **not met** — remedy "ready the PR and arm auto-merge"; high |
| `has_repo` and goal looks like code work, no PR | **not met** — remedy "open a PR and arm auto-merge"; **low** |
| not `has_repo` (or goal clearly non-code), no PR | met — research escape (low) |

"Goal looks like code work" is a conservative keyword/heuristic helper
(`_is_code_goal(goal)`) used **only** to pick the last two rows apart. On the live
path the LLM decider owns this nuance (see Unit 2); the heuristic exists so the retro
pass (no LLM) can still classify, and it deliberately routes uncertain cases to `low`
confidence → report-only, never auto-unarchive.

### Unit 2 — Live wiring (`server/sessions/orchestrator.py`, `autopilot_*`)

1. **Assemble `pr_facts` in `_autopilot_get_context`.** Reuse the pattern from
   `request_pr_fix`: iterate `rec.authored_branches` (∪ `rec.branch`), look up each in
   the poller cache via `self.pr_for(repo, br)`. No new I/O — cache only. Set
   `has_repo = bool(rec.repo)`. Attach a new `pr_facts` field to `DecisionContext`.
   > Note: `authored_branches` excludes the scratch branch, so a "wrote code but never
   > opened a PR" session yields empty `prs`. That case is intentionally left to the
   > fact-fed decider (it sees `has_repo` + goal), not the deterministic gate.

2. **Decider guidance** (`autopilot_guidance.md`) rewritten to *use* `pr_facts`:
   - complete only when `pr_facts` shows an armed or merged PR, **or** the goal is
     clearly non-code and no PR is expected;
   - a code-oriented session (`has_repo`) with no armed PR → `nudge` toward opening +
     arming the PR, **never** `complete`.
   `pr_facts` is added to the decider prompt payload in `autopilot_decider._build_prompt`.

3. **Deterministic hard gate in `post_validate`** (narrow, false-positive-free):
   if `decision.action == "complete"` and `ctx.pr_facts.open_unarmed_pr` is not None,
   downgrade to `nudge` with the remedy text. The LLM cannot override this — an
   un-armed open PR is definitionally not the finished state per CLAUDE.md's
   completion workflow. The "no PR at all" case stays with the decider (step 2),
   because "just opened, not cached yet" vs "never opened" vs "research" needs goal
   judgment the pure gate can't do safely.

### Unit 3 — Retro audit (`server/automations/archive_audit.py` + entrypoint)

An offline audit that reuses `met_bar`:

1. List archived sessions (`orchestrator.list_archived`), reading each row's
   `authored_branches`, `branch`, `repo`, `active_goal`/`inferred_goal`/`name`.
2. For each, build `PrFacts` from **real `gh` queries** (not the live cache — archived
   branches aren't polled): `gh pr list --state all --head <branch> --repo <repo>
   --json number,state,isDraft,autoMergeRequest,url` over the authored branches (∪
   scratch branch). Merged PRs are found here → those sessions are LEGIT.
3. Classify via `met_bar` into three buckets:
   - **LEGIT** (`met`) → leave archived.
   - **PREMATURE** (`not met` and `confidence == 'high'`, i.e. an open-unarmed PR
     exists) → **auto-unarchive** via `orchestrator.unarchive` and record an audit
     entry.
   - **AMBIGUOUS** (`not met` and `confidence == 'low'`, code-ish goal but no PR at
     all) → **report only**, no auto-move (a human decides).
4. Emit a report table: `session | goal | repo | PR state | verdict | action`.

**Delivery:** a daemon-side entrypoint (a `python -m server.automations.archive_audit`
module / management command) that prints the report and performs the unarchiving.
`--dry-run` prints without moving. It talks to the same DB + orchestrator seams the
daemon uses. (Surfacing the report in the Automations UI feed is a possible later
add; out of scope here.)

## Data flow

**Live (per governed session, per tick):**
`_autopilot_get_context` → build `pr_facts` from poller cache → `DecisionContext` →
`pre_decide` (unchanged) → `decide` (now sees `pr_facts`) → `post_validate` (hard gate
downgrades premature `complete`) → `_advance_one` (archive only on a surviving
`complete`).

**Retro (one-off):**
`list_archived` → per session `gh pr list --state all` → `PrFacts` → `met_bar` →
bucket → unarchive PREMATURE / report all.

## Error handling

- **Stale/missing PR cache (live):** absent `pr_facts` never *forces* an archive — the
  decider errs to `nudge`/`escalate` (its existing "bias to escalate when unsure"), and
  the hard gate only fires on positive un-armed evidence. Not archiving is the safe
  direction.
- **`gh` failure (retro):** a session whose PR lookup errors is bucketed AMBIGUOUS
  (report-only), never auto-unarchived. Best-effort per session; one failure never
  aborts the audit (mirrors `batch_unarchive_sessions`).
- **Unarchive failure (retro):** logged, recorded as failed in the report, audit
  continues.

## Testing

Pure unit tests (no network/model), matching the existing `test_autopilot_*` style:

- `test_completion_bar.py` — the full `met_bar` truth table: merged→met, armed→met,
  open-unarmed→not-met(high), code-goal+no-PR→not-met(low), research+no-PR→met(low).
- `test_autopilot_policy_rules.py` (extend) — `post_validate` downgrades `complete`→
  `nudge` when `pr_facts.open_unarmed_pr` present; leaves `complete` when armed.
- `test_autopilot_get_context` (new/extend) — `pr_facts` assembled from a fake poller
  cache over `authored_branches`; `has_repo` reflects `rec.repo`.
- `test_autopilot_decider.py` (extend) — `_build_prompt` includes `pr_facts`; a reply
  is still parsed.
- `test_archive_audit.py` — with an injected `_gh_fn` (never calls real `gh`, mirrors
  the decider's `_query_fn` seam): merged PR → LEGIT/left; open-unarmed → PREMATURE/
  unarchived (asserts `unarchive` called); code-goal+no-PR → AMBIGUOUS/report-only;
  `gh` error → AMBIGUOUS. `--dry-run` performs no unarchive.

CI gates (`ruff`, `mypy`, `pytest`) must be green; `mypy` is an easily-missed required
gate.

## Constitution / working-norm compliance

- **VI (single source of truth):** `met_bar` is the one definition of "done", shared by
  live + retro. No parallel copy.
- **III (verify against reality):** completion keys on real `gh`/poller PR state, not
  transcript inference.
- **II (isolation):** retro `gh` runs read-only; auto-unarchive uses the existing
  reversible `unarchive` seam.
- **Testability:** every I/O dependency (`gh`, model) is injected, so the whole
  pipeline is unit-tested with no network — the proven `autopilot_decider._query_fn`
  pattern.
