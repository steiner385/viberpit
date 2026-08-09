# Autopilot Contextual Nudges — Design

**Date:** 2026-08-09
**Status:** Approved
**Problem owner:** autopilot (`server/automations/`)

## Problem

Autopilot sends canned, repetitive, and frequently *wrong* direction to governed
sessions instead of context-aware guidance. Evidence from production
(`~/.claude-accounts/ccswitch.db` + session transcripts, 2026-08-09):

- The literal template string **"open a PR and arm auto-merge before finishing"
  was sent 6,936 times** across session transcripts. It is the
  `completion_bar.met_bar` low-confidence remedy, injected verbatim by the
  `post_validate` completion-gate downgrade — a deterministic path that bypasses
  the LLM decider entirely.
- Individual sessions received **38–39 nudges in 3 days**, tick after tick, with
  no memory of what was already said.
- Many nudges were factually wrong: `pr_facts` (PR poller cache) lagged or missed
  branch linkage, so sessions that had *already armed or merged* a PR were told
  to open one. Transcripts show sessions arguing back: "No. PR #12294 is merged.
  Not creating a duplicate.", "PR #12301 is already in the merge queue at
  position 1 — the nudge is a false positive from the session tracking".
- The loop-breaker (`fingerprint` → escalate after `loop_repeat_limit` repeats)
  fails to contain this: the no-concrete-interaction fingerprint keys on
  `effective_goal`, and goal re-inference mutates the goal text, resetting the
  repeat counter.
- By contrast, the nudges the Haiku decider composed itself were largely good
  and specific ("Verify PR #13228 state with `gh pr view …`", "Continue
  implementing #13269 … the transcript shows you're mid-investigation…").

Ranked pain (user-confirmed): (1) repetitive nudges, (2) generic unspecific
nudges, (3) wrong-state nudges. Question-answering quality was explicitly *not*
flagged.

## Goals

1. No canned nudge text ever reaches a session.
2. Direction is composed from the session's real state: transcript, verified PR
   facts, fleet context, and what autopilot already said.
3. Wrong-state nudges become rare: contradictions between transcript claims and
   the PR cache are *verified against GitHub* before any direction is issued.
4. Repetition is bounded deterministically, keyed on what was actually sent.

## Non-goals

- Question-answering (`answer` action) quality changes.
- `pr_intent` classifier changes.
- PR poller architecture changes beyond a rate-limited on-demand refresh call.

## Design

### 1. Completion-gate keeps its verdict, loses its voice

`post_validate`'s completion-gate still prevents a not-met goal from completing
(deterministic authority unchanged). But it no longer fabricates nudge text.
Instead of `Decision(action="nudge", prompt=<canned remedy>)` it returns a
decision marked **`needs_composition=True`** carrying the `BarVerdict`
(reason + remedy) as *structured context*. The final nudge text is always
written by the composer (section 3). `met_bar`'s remedy strings survive only as
hints in the composer prompt, never as the message.

### 2. Verify before directing (on-demand PR-facts refresh)

Trigger: the gate downgrades a `complete`, or a nudge is about to be composed on
the "no armed/merged PR" branch, **and** either (a) the transcript tail claims a
PR number that `pr_facts` doesn't show, or (b) the session status is `complete`.

Then, before composing:

- `await refresh_authored_branches()` and
  `await poller.refresh_once(<this session's (repo, branch) pairs>)` —
  rate-limited to **once per session per 10 minutes** via a
  `last_facts_refresh` timestamp in the session's automation ledger, and
  skipped while the poller is in failure cooldown (respect `_cooldown_until`).
- Also consult the population cache (`_pr_population_pr`) so a PR missed by
  `authored_branches` linkage is still found.
- Re-run `met_bar` on the fresh facts:
  - Goal now met → the original `complete` stands (archive / propose as today).
  - Still contradicted → compose a *reconciliation* nudge; the composer prompt
    states explicitly: "verified GitHub state as of `<ts>`: no PR on branches
    X, Y; the transcript claims PR #N — direct the session to reconcile."

Only a **verified** contradiction produces a reconciliation nudge (when the
refresh is unavailable — rate-limited or poller cooldown — see Error handling:
compose against stale facts, saying so). The refresh reuses the existing
`PrPoller.refresh_once` chunked-GraphQL path — no new GitHub client.

### 3. Two-tier decider: Haiku gate → Sonnet composer

New module `server/automations/nudge_composer.py`, mirroring
`autopilot_decider.py`'s house pattern: injected `_query_fn` (the real model is
never called in tests), lazy SDK options only on the real path, safe failure
mode.

- **Gate (Haiku, unchanged):** decides
  `approve|deny|answer|complete|escalate|nudge` from today's compact context.
  Its `prompt` field is demoted to a one-line *intent hint* for the composer.
- **Composer (Sonnet, runs only when the resolved action is `nudge`):** inputs:
  - a **larger transcript window**: ≈40 entries × 1,500 chars, tool-call heads
    at 400 chars (`_autopilot_transcript_tail` is already parameterized);
  - effective goal + source, verified `pr_facts` + refresh timestamp,
    `BarVerdict` hint, fleet view, reversal hint;
  - **prior-nudge history** (section 4).
  - Output contract (enforced by `nudge_composer_guidance.md`): name concrete
    artifacts (files, failing test names, PR numbers) taken from the actual
    transcript; state the next step and what "done" looks like; never restate
    the generic tests→commit→push→PR pipeline; if there is nothing concrete to
    say, return `{"escalate": true}` instead of filler.
- **Composer failure or escalate output → escalate** the session. A human beats
  a canned string; there is no template fallback.

Cost note: the gate runs at today's frequency and price; Sonnet runs only on
nudge compositions, which are far rarer than gate evaluations and already
budget-capped (12 actions/session/hr, 300/day global).

### 4. Anti-repetition memory

The per-session ledger (`automation_target_state`) gains
`recent_nudges: [{ts, text}]`, capped at the last 5, pruned on write.

- **Composer input:** the history appears in the prompt with the instruction:
  "these were already sent; if the session didn't follow the last one, change
  strategy or escalate — do not rephrase it."
- **Hard dedupe (deterministic; the model is not trusted):** before sending, a
  normalized token-set similarity check compares the composed prompt against
  the nudges sent in the last 6 hours. Near-identical → the send is
  **suppressed** and recorded as a fingerprint repeat, so loop-escalation
  climbs. This closes the fingerprint-reset hole: repetition is now keyed on
  *what autopilot said*, not on goal text that re-inference keeps mutating.
  Threshold: token-set ratio ≥ 0.85 counts as near-identical (module constant,
  unit coverage on the boundary).

### 5. Guidance rewrite

- `autopilot_guidance.md` (gate): the nudge section drops the prescriptive
  pipeline recipe; a nudge decision means "signal intent — the composer writes
  the words." Completion logic and fleet/reversal guidance stay.
- New `nudge_composer_guidance.md`: carries the composer contract (concrete
  artifacts, next step + done-looks-like, no pipeline parroting, escalate over
  filler, respect prior nudges).

## Data flow (changed path only)

```
tick → pre_decide (unchanged guardrails)
     → gate (Haiku) → action
        action != nudge → post_validate → apply (unchanged)
        action == nudge, or post_validate downgrade (needs_composition):
            contradiction? → rate-limited refresh_once + population-cache check
                           → re-run met_bar → maybe complete after all
            → composer (Sonnet, rich context + nudge history)
                 → escalate (no filler) | prompt
            → hard dedupe vs recent_nudges
                 near-identical → suppress + fingerprint repeat
                 fresh → send_prompt, append to recent_nudges
```

## Error handling

- Refresh failure / poller cooldown → compose against stale facts but say so in
  the composer prompt ("facts may lag; prefer a verify-with-gh nudge").
- Composer failure → escalate (never send a template).
- Ledger read/write failures → best-effort, mirroring existing `_charge`
  semantics; a failed history write must not block a decided action.

## Testing

- Pure-logic unit tests: contradiction detection, dedupe similarity boundary,
  refresh rate-limit gating, `needs_composition` routing through
  `post_validate`.
- Gate/composer tested with injected `query_fn` fakes (house pattern; the real
  model is never called).
- Loop-level tests:
  1. stale-facts `complete` + fresh-facts armed PR → archives, no nudge;
  2. near-identical composed nudge → suppressed, and escalates once the
     repeat limit is reached;
  3. composer failure → escalation, not a canned send.

## Out of scope / future

- Smarter `answer` decisions.
- Learning from nudge outcomes (which nudges get followed) beyond the existing
  reversal-hint mechanism.
