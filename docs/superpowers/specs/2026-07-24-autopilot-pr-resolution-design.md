# State-aware autopilot: PR-resolution for finished / cancelled sessions

**Date:** 2026-07-24
**Status:** Approved (design) — implementation pending
**Author:** ccswitch autopilot work

## Problem

The autopilot repeatedly instructs running sessions to "open a PR and arm auto-merge"
even when the session has already **finished the work without needing a PR** or has
**explicitly decided to cancel / abandon its PR work**. It re-derives this nudge every
tick, so a session that has legitimately decided "no PR here" is badgered indefinitely
and never archives.

### Root cause

Two judgment layers both push toward "open a PR," and neither can see the session's
*intent*:

1. `server/automations/completion_bar.py::met_bar()` — the single source of truth for
   "is the goal met?" — judges purely from **GitHub PR facts** (precedence:
   `merged > armed > open-unarmed > no-PR`). A code goal with no PR yields
   `met=False, remedy="open a PR and arm auto-merge before finishing"`.
2. `server/automations/autopilot_policy.py::post_validate()` (lines 177-190) is a
   deterministic backstop: when the decider says `complete` but `met_bar` returns
   `met=False`, it **downgrades to a nudge** carrying that remedy string.

The only session-intent signal the decider sees is a **6-turn / 400-char transcript
tail** (`_autopilot_transcript_tail`). A decision made many turns ago
("we're not opening a PR for this") has scrolled out of that window, while "no PR
exists" is a **durable, always-visible** GitHub fact. The durable fact wins every time.

**The fix:** give the system a *durable* record of a session's PR-work decision, so the
"no PR exists" branch can be short-circuited when the session has resolved that it will
not open one — and archive such sessions at the right time.

## Design

### 1. Durable state (data model)

Add two **nullable** columns to the session model (`server/db/models.py::DbSession`)
and mirror them on the in-memory `SessionRecord`. Nullable is required: dev SQLite's
`init_db` backfills missing nullable columns but never ALTERs to add a NOT NULL one
(see CLAUDE.md dev notes).

- `pr_resolution: str | None` — `None` = unresolved (today's behavior). Set to a short
  reason token: `"cancelled"` (session decided not to open a PR) or `"not-needed"`
  (work is done and no PR is warranted). Free-text detail may be appended after a colon
  for audit, e.g. `"cancelled: user asked to drop the change".`
- `pr_resolution_source: str | None` — `"marker"` (deterministic) or `"classifier"`
  (probabilistic). Distinguishes how much to trust the value and aids audit.

This mirrors the existing sticky-field pattern (`autopilot_paused`,
`auto_archive_opt_out_reason`).

### 2. How `pr_resolution` gets set

**(a) Explicit marker — deterministic.** A single new marker in
`server/sessions/completion.py`:

```
NO_PR_MARKER = "<!-- ccswitch:no-pr -->"
```

Its instruction (added to the completion instruction the session's system prompt
carries) tells the agent: when the work is genuinely done but no PR is appropriate — or
you have decided not to open one — end your final message with this marker (optionally
followed by a short reason). `translator.py` (which already scans `self._turn_text` for
`COMPLETION_MARKER` at line 151) also detects `NO_PR_MARKER`: presence implies the turn
is **complete** *and* carries a PR resolution. The resolution is threaded to the
orchestrator (via the turn-end path that already carries `outcome`) and persisted to
`pr_resolution` (source `"marker"`) through the `_persist_status` driver callback — never
written on the WS-attach coroutine (per the detached-attach invariant).

*Granularity decision:* **one marker + free-text reason**, not two markers. Both
`cancelled` and `not-needed` drive identical autopilot behavior; the reason string is
enough for audit. The marker defaults its reason token to `"not-needed"` unless the
session's text clearly indicates cancellation.

**(b) Classifier — probabilistic fallback.** A new pure-ish module
`server/automations/pr_intent.py` exposes an async
`classify_pr_intent(transcript_window, goal) -> PrIntentVerdict` that calls the cheap
Haiku model (reusing the decider's LLM plumbing) over a **widened** transcript window
(substantially larger than the decider's 6-turn peek). It returns:

```python
@dataclass(frozen=True)
class PrIntentVerdict:
    resolved: bool          # True => the session has decided NOT to open a PR
    reason: str             # "cancelled" | "not-needed" | "" (unresolved)
    confidence: str         # "high" | "low"
```

Only a `resolved=True, confidence="high"` verdict is acted upon.

### 3. Where the classifier fires (gating)

The classifier runs during **async context assembly** (`_autopilot_get_context`), never
inside the pure synchronous `post_validate`. Sequence:

1. Assemble `pr_facts` as today.
2. If `pr_resolution` is already set (marker or a prior classifier run), skip the
   classifier — the durable value stands.
3. Otherwise compute `met_bar(goal, goal_source, pr_facts)`. **Only** when the verdict
   is the low-confidence "code goal but no PR opened" branch (the exact branch that
   produces the buggy nudge) do we invoke `classify_pr_intent` over the widened window.
4. On `resolved=True, confidence="high"`, persist `pr_resolution` (+ source
   `"classifier"`) and expose it on the `DecisionContext`.

This keeps all I/O in the async layer, leaves `post_validate` a pure function reading a
durable field, and fires the classifier **rarely** — only at the one decision point
where PR-cancellation intent changes the outcome.

### 4. How it changes the decision

- **`met_bar()`** gains a `pr_resolution: str | None` parameter. New precedence:
  `merged > armed > open-unarmed > pr_resolution(resolved) > no-PR-nudge`.
  Real PR facts still win: a `pr_resolution` value is **ignored (and treated as stale)**
  whenever an actual PR exists for the session — merged/armed complete as before, and an
  open-unarmed PR still nudges "arm it." `pr_resolution` short-circuits **only** the
  "no PR exists at all" branch, returning `met=True, reason="session resolved no PR
  needed", confidence="high"`.
- **`post_validate()`** now passes `ctx.pr_resolution` into `met_bar`, so a resolved
  session yields `met=True` and the `complete` decision is **no longer downgraded** to
  the open-a-PR nudge.
- **`autopilot_guidance.md`** gains a line instructing the decider to respect a session
  that has declined / finished-without a PR (belt-and-suspenders; the deterministic gate
  already covers it).

**Stale-resolution override (decision):** yes — real PR facts always win. If a PR later
appears for a session already marked `pr_resolution`, `met_bar` ignores the resolution.
The classifier never runs while a PR exists (step 3 only fires on the no-PR branch), so a
stale value can only arise if a PR is opened *after* resolution; the `met_bar` precedence
handles that safely without needing to actively clear the column.

### 5. Auto-archive

Extend `_maybe_autoarchive_completed()`
(`server/sessions/orchestrator.py:1308`):

- **Broaden the origin gate.** Today it archives only `origin == "backlog-runner"`.
  Broaden to **any autopilot-driven session that is not paused** (per the approved
  scope): a session is eligible when it is being governed by the autopilot loop and
  `autopilot_paused` is falsy. Backlog-runner remains eligible (it is autopilot-driven).
  Interactive sessions the autopilot is *not* governing are still left alone.
- **Add a trigger.** A session whose `pr_resolution` is set is in a terminal, resolved
  state → schedule auto-archive, still honoring the sticky `auto_archive_opt_out_reason`
  opt-out and the `_auto_archiving` idempotency guard. Because the classifier only
  persists on high confidence and the marker is deterministic, the presence of
  `pr_resolution` is a sufficient gate.
- The existing 6-hour `archive_audit` re-judge loop stays a safety net. Because it also
  routes through `met_bar` — which now reads `pr_resolution` — it will **not**
  immediately un-archive these sessions (no divergence between the live decision and the
  retro audit). Its snapshot `archive_context` records the resolution for audit.

### 6. Component boundaries

| Unit | Responsibility | Depends on |
|------|----------------|------------|
| `completion.py` | Defines `NO_PR_MARKER` + instruction text (SoT) | — |
| `translator.py` | Detects marker in final turn text → resolution signal | `completion.py` |
| `pr_intent.py` (new) | `classify_pr_intent()` LLM verdict over wide window | decider LLM plumbing |
| `completion_bar.met_bar()` | Pure verdict incl. `pr_resolution` precedence | — |
| `autopilot_policy.post_validate()` | Passes resolution to `met_bar`; no PR-nudge when resolved | `completion_bar` |
| `orchestrator._autopilot_get_context()` | Gated classifier call + persist resolution | `pr_intent`, `completion_bar` |
| `orchestrator._maybe_autoarchive_completed()` | Broadened gate + resolution trigger | — |
| `db/models.py` / `SessionRecord` | Durable `pr_resolution*` columns | — |

## Error handling

- Classifier failure returns an **unresolved** verdict (mirrors the `reversal_hint` /
  `fleet_context` degradation contract): the decision still runs, autopilot just falls
  back to today's behavior. A classifier failure must never stall a tick.
- Marker detection is best-effort string matching; a malformed reason falls back to the
  `"not-needed"` token.
- Auto-archive keeps the existing failure path: on archive failure, clear the
  `_auto_archiving` guard so a later emit can retry.

## Testing (TDD)

Unit tests, written before implementation:

1. `completion.py` / `translator.py`: `NO_PR_MARKER` in final turn text yields a
   complete turn carrying a `not-needed` resolution; cancellation phrasing yields
   `cancelled`; absence yields no resolution.
2. `met_bar()` precedence: `pr_resolution` set + no PR → `met=True` high-confidence;
   `pr_resolution` set + **merged/armed/open-unarmed PR present** → PR facts win
   (resolution ignored); no resolution → today's behavior unchanged.
3. `classify_pr_intent()`: high-confidence cancel/not-needed → `resolved=True`; ambiguous
   → `resolved=False`; LLM error → unresolved (no raise).
4. Gating: the classifier is invoked **only** on the no-PR branch and **not** when a PR
   exists or `pr_resolution` is already set.
5. `post_validate()`: a `complete` decision with `pr_resolution` set is **not** downgraded
   to a nudge; without it, the existing downgrade still fires.
6. `_maybe_autoarchive_completed()`: fires for a non-backlog autopilot session on
   `pr_resolution`; respects `auto_archive_opt_out_reason`; does not fire for a paused or
   non-governed session.

## Out of scope / YAGNI

- No UI surface for `pr_resolution` in this change (audit via DB / activity feed is
  enough for v1).
- No new config knobs; reuse existing autopilot config and the archive-audit loop.
- No change to how PRs are actually detected (poller cache remains the PR-facts source).
