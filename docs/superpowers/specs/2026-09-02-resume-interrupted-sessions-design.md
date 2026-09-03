# Batch & Auto Resume of Interrupted Sessions — Design

**Date:** 2026-09-02
**Status:** Approved design, pending implementation plan

## Problem

After a daemon deploy/restart, every session that was mid-turn is normalized to `idle` and shown as
**"Interrupted — re-send to resume"** (`interrupted_by_restart=True`). The operator must click into
each one and manually re-send a prompt to continue the dropped work. On a busy fleet that is a pile
of manual clicks after every deploy (see the Sessions screenshot: 5 sessions stranded at once,
several of them unattended automation runs).

We want two things:
1. A **batch action** to resume all currently-interrupted sessions in one click.
2. An **opt-in auto-resume on restart** so, when enabled, interrupted sessions self-heal without any
   click at all.

## What already exists (verify-against-reality, Principle III)

- **The resume primitive is already in the codebase.** Autopilot's "nudge" action is exactly
  "re-attach a detached session and send it a prompt": `await ensure_driver(id, SYSTEM_IDENTITY);
  await send_prompt(id, decision.prompt)` (`server/sessions/orchestrator.py:4673`). `ensure_driver`
  resumes the `claude --resume <claude_session_id>` process for a detached session; `send_prompt`
  drives the turn. We are **exposing** this pattern, not inventing it.
- **The clean-drain case is already auto-resumed.** On SIGTERM the daemon snapshots each in-flight
  prompt to `pending_prompt` and background tasks to `pending_rearm`; on startup
  `rearm_background_tasks()` replays them (`orchestrator.py:5909`, gated by
  `CCSWITCH_REARM_CONCURRENCY`). The "Interrupted — re-send" state is precisely the case with **no
  snapshot to replay** — a hard kill/crash, or a turn that had already emitted durable output so no
  `pending_prompt` was saved.
- **`interrupted_by_restart` is a transient in-memory flag**, set in `rehydrate()` when the
  pre-restart status was in `_RESTART_TRANSIENT_STATUSES` (`orchestrator.py:5834`). It is never
  persisted; it clears on the next non-idle status (`orchestrator.py:2167`).
- **Origin marks automation vs human.** `SessionRecord.origin` / DB `origin` is the automation_id
  that spawned the session (NULL = human) (`server/db/models.py:199`). Automation sessions are **not**
  re-driven by the autopilot/backlog loops after a restart — they sit interrupted like human ones.
- **Batch endpoints have an established shape**: `POST /api/sessions/batch/{archive,unarchive,
  permanent-delete}` loop per-id, best-effort, return `{succeeded, failed}` with
  `BatchSessionIdsRequest {ids: [str], max_length=200}` (`server/api/sessions.py:744,810`).

## Decisions (locked with the user)

| Decision | Choice | Implication |
|---|---|---|
| Resume semantics | **Re-attach + nudge** | `ensure_driver` then `send_prompt(nudge)`. A bare re-attach would leave the dropped turn un-continued. |
| Mechanisms | **Both** | Batch UI button **and** opt-in auto-on-restart, over one shared primitive. |
| Nudge text | **Single explicit restart note** | Same text for human and automation: tells the model why it is being re-driven. No per-origin branching. |
| Auto-on-restart scope | **All eligible** (human + automation) | When the flag is on, a deploy resumes human sessions too. Flag defaults **off**, so the batch button is the always-available manual path. |
| Auto-on-restart gate | **`CCSWITCH_AUTO_RESUME_ON_RESTART`, default off** | Opt-in; high blast radius on every deploy, so not on by default. |

## Eligibility (the safety core)

`interrupted_by_restart` alone is too broad: `_RESTART_TRANSIENT_STATUSES` includes states where
"continue" is the *wrong* nudge. A session is **resume-eligible** iff `interrupted_by_restart` is set
AND its **original pre-restart status** was a *work* state — `in_progress` / `working` / `condensing`
— and it is **not**:

- `ready_review` — the turn already finished and is awaiting review;
- `waiting_question` / `waiting_input` — it was waiting on *the operator*, not stalled work;
- `limited` — account is rate-limited; a nudge just re-hits the wall;
- `waiting_external` — waiting on CI/external, not on being nudged;
- archived / ended / parked.

Both the batch button and auto-resume use the **same** eligibility predicate so the button's count is
honest and the two paths never diverge.

### Capturing the original status

`rehydrate()` currently normalizes the pre-restart status to `idle` and discards the original. We add
a **transient, memory-only** field `SessionRecord.interrupted_from_status: str | None` (set alongside
`interrupted_by_restart`, never persisted, cleared with it). It exists solely to drive the
eligibility predicate — it does **not** change the "Interrupted — re-send to resume" badge, which
still shows for the full transient set.

## Architecture

Three units, each with one purpose.

### 1. Shared primitive — `orchestrator.resume_interrupted`

```python
async def resume_interrupted(
    self, session_id: str, identity: Identity, *, nudge: str
) -> str:  # "resumed" | "skipped:<reason>" | "no_driver"
```

- Look up the record; if not resume-eligible (per predicate above) return `"skipped:<reason>"`.
- `await self.ensure_driver(session_id, identity)`.
- `await self.send_prompt(session_id, nudge)`; on `"sent"` the next-status transition clears
  `interrupted_by_restart` / `interrupted_from_status` via the existing path (`orchestrator.py:2167`).
- Single source of truth for both entry points (Principle VI). The nudge text lives as one module
  constant, e.g. `RESUME_NUDGE = "Your previous turn was interrupted by a daemon restart. Please
  continue where you left off."`

An eligibility helper — `_resume_eligible(rec) -> str | None` (returns a skip-reason or `None` when
eligible) — is shared by the primitive, the batch endpoint's candidate list, and the auto pass.

### 2. Batch entry point — `POST /api/sessions/batch/resume`

- Mirror `batch/archive` exactly (`server/api/sessions.py:810`): `BatchSessionIdsRequest`, per-id
  try, return `{succeeded: [ids], failed: [{id, error}]}`; a skip returns as a `failed` entry with
  `error="skipped:<reason>"` (best-effort, one failure never aborts the batch).
- **Frontend:** a `useResumeInterrupted` mutation hook mirroring `useSendPrompt`
  (`web/src/api/hooks.ts:288`) that posts the list and invalidates `QK.sessions`. A **"Resume
  interrupted (N)"** control next to the existing "Archive done (11)" affordance, where N is the count
  of currently resume-eligible sessions (computed client-side from the session list using a predicate
  that mirrors the server's — the server predicate remains authoritative). Hidden when N is 0.

### 3. Auto entry point — startup pass folded into rearm

- In the daemon lifespan, after `rehydrate()` + `rearm_background_tasks()` (`server/ipc/serve.py:109`),
  run an auto-resume pass **only if** `CCSWITCH_AUTO_RESUME_ON_RESTART` is truthy.
- Reuse the existing `CCSWITCH_REARM_CONCURRENCY` bounded-concurrency helper to iterate eligible
  sessions and call `resume_interrupted(id, SYSTEM_IDENTITY, nudge=RESUME_NUDGE)` — this gives a
  built-in thundering-herd guard identical to rearm.
- Ordering: run **after** rearm so a session that already had a `pending_prompt` replay is no longer
  interrupted and is naturally skipped (not double-driven).

## Interaction & correctness notes

- **No double-drive with autopilot.** The autopilot loop governs sessions on its own tick; because
  `resume_interrupted` sends a real prompt that flips the session out of `idle`, the flag clears and a
  subsequent autopilot decision sees a live, non-interrupted session — no second nudge for the same
  interruption.
- **Idempotent batch.** Re-clicking after resume: the now-live sessions fail the eligibility predicate
  and come back as `skipped:not-interrupted`, so a double click is a no-op, not a double nudge.
- **Detached-but-eligible.** `ensure_driver` is the same resume path used everywhere; if the account
  is unavailable it surfaces `no_driver` and the batch reports it as a failure rather than throwing.

## Testing

- **`resume_interrupted` unit tests** (server): eligible work-state → `ensure_driver` + `send_prompt`
  called, returns `resumed`; each excluded status (`ready_review`, `waiting_question`, `limited`,
  `waiting_external`, archived) → `skipped:<reason>`, no driver spawned; `no_driver` propagated.
- **Eligibility predicate** table-driven test over every `_RESTART_TRANSIENT_STATUSES` value + the
  exclusions.
- **`rehydrate` captures `interrupted_from_status`** for a mid-turn row and clears it with the flag.
- **Batch endpoint** test mirroring the `batch/archive` test: mixed eligible/ineligible ids →
  `{succeeded, failed}` partition; unknown id → failed entry.
- **Auto pass gated** by `CCSWITCH_AUTO_RESUME_ON_RESTART`: off → no resume calls; on → eligible
  sessions resumed, ineligible skipped, runs after rearm.
- **Frontend:** `useResumeInterrupted` posts the eligible id list; the button shows the right N and
  hides at 0 (vitest + RTL, mirroring the batch-archive UI test).

## Out of scope (YAGNI)

- Per-origin / automation-specific nudges (locked to a single text).
- Persisting `interrupted_by_restart` or a resume audit trail beyond existing logging.
- Auto-resume of `limited` / `waiting_external` sessions (deliberately excluded).
- Any change to the clean-drain `pending_prompt` rearm path — it already works.

## Constitution check

- **VI (single source of truth):** one `resume_interrupted` primitive + one eligibility predicate
  behind both entry points; no parallel copy of the nudge logic.
- **II (per-process isolation):** resume goes through the existing `ensure_driver` →
  `claude --resume` path; no new process-ownership model.
- **IV/V (security-first, quality gates):** auto path is opt-in and default-off; full test matrix
  above; the four-step `lint-and-test` gate applies (ruff, mypy, core pytest, server pytest).
