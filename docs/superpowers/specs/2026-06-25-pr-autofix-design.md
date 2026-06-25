# Auto-restore failing PRs — Design

**Date:** 2026-06-25
**Status:** Approved (brainstorming) → ready for implementation plan
**Roadmap:** Realizes the action half of combined-app milestone **M3 "PR Follow-through"**
(react to PR breaks, route back to owning session by live branch).

## Problem

When a PR associated with a session fails CI / needs attention, the system today only
*detects* the failure: `_on_pr_change()` (`server/sessions/orchestrator.py`) fires when a PR's
checks transition non-failing → failing and sends a "PR broke" push notification. Nothing acts on
it — the user must notice the notification, reopen the owning session, remember which branch the PR
was on, and drive the fix by hand.

We want the system to **react**: optionally surface a one-tap fix action, or autonomously inject a
fix prompt into the owning session, so a broken PR gets investigated and restored without manual
shepherding.

## Goals

- A global, user-configurable policy controlling how aggressively ccswitch responds to a failed PR.
- Reuse the existing detection (`_on_pr_change`), prompt-injection (`send_prompt`), settings, and
  suggestion surfaces — no parallel implementations (constitution VI).
- Be safe: never interrupt an in-progress turn, never loop on a PR it can't fix, never act on
  terminal (merged/closed) PRs.

## Non-goals

- No new session-spawning: the **owning** session does the work, not a fresh dedicated session.
- No per-session configuration — policy is global (one `AppSettings` field).
- No change to how PR failure is *detected* (the GraphQL poller + `_on_pr_change` stay as-is).
- No bounded multi-retry loop — exactly one auto-attempt per distinct failure event (see Loop guard).

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Autonomy model | Global setting, not per-session |
| Setting values | `off` / `propose` / `auto` |
| Default value | `propose` |
| Which session does the work | The **owning** session (matched by `(repo, branch)`) |
| Busy owning session (auto mode) | Queue behind the current turn (reuse driver FIFO `_queue`) |
| Owning session has no live driver (auto mode) | Fall back to notify/propose — do **not** auto-resume |
| Loop guard | One attempt per failure event; re-arm only after checks go green/merged |

## Architecture

### 1. Configuration — extend `AppSettings`

Add one field to the existing singleton settings row; do **not** build new settings plumbing.

- **DB** (`server/db/models.py` `AppSettings`): new column `pr_autofix_mode: Mapped[str | None]`,
  **nullable** (the `init_db` backfill cannot add NOT NULL columns to an existing table — see
  CLAUDE.md "Dev / testing"). Code treats `NULL`/unknown as the default `propose`.
- **API** (`server/api/settings.py`): include `prAutofixMode` in the GET response and accept it in
  PUT, validating against `{"off", "propose", "auto"}` and silently ignoring invalid values (same
  guard pattern as `defaultPermissionMode`).
- **Frontend** (`web/src/api/hooks.ts`): add `prAutofixMode` to the `AppSettings` interface;
  `useSettings`/`useUpdateSettings` already carry arbitrary fields.
- **Frontend UI** (`web/src/components/settings/SettingsPanel.tsx`): a new dropdown
  "PR auto-fix" with options Off / Propose / Auto, mirroring the existing permission-mode dropdown.

Mode meanings:

- **off** — unchanged from today: push the "PR broke" notification, nothing more.
- **propose** — additionally surface a one-tap **"Fix PR #N"** action on the failing-PR session.
- **auto** — inject the fix prompt into the owning session automatically (with the loop guard and
  no-driver fallback below).

### 2. One shared action, two triggers

A single orchestrator method is the source of truth for "restore this PR":

```
request_pr_fix(session_id, repo, branch, pr) -> result
```

It builds the fix prompt (§3), applies the loop guard (§4), and injects via the existing
`send_prompt()` path — which already enqueues behind an in-progress turn (driver FIFO `_queue`) and
returns a delivery status (`sent` / `draining` / no-driver). Two callers route through it:

- **auto trigger:** inside `_on_pr_change()` — which already resolves the owning session from
  `(repo, branch)` — when `pr_autofix_mode == "auto"`. If `send_prompt` reports no live driver (or
  draining), fall back to the notification/proposal rather than resuming the driver.
- **propose trigger:** a new authenticated endpoint `POST /api/sessions/{id}/fix-pr` (body carries
  the target PR number/branch), hit when the user taps "Fix PR #N". It calls the same
  `request_pr_fix`.

Because both paths converge on `request_pr_fix`, the user-tapped fix and the autonomous fix produce
identical agent behavior — no second implementation to drift.

### 3. The fix prompt — branch-aware

`build_pr_fix_prompt(repo, pr)` returns a system-originated prompt. It must **not** assume the
worktree is already on the PR's branch: ccswitch's completion workflow moves the worktree onto a
fresh `feat/*` branch after opening a PR (see memory `pr-join-branch-must-be-live`), so an older
PR's HEAD has usually moved on. The prompt instructs the agent to:

1. Check out the PR's `headRefName` (`gh pr checkout <number>` or `git checkout <branch>` + pull).
2. Inspect the failing checks (`gh pr checks <number>`; reproduce the failing job locally).
3. Fix the cause, commit, and push so CI re-runs.
4. Report what was broken and what it changed.

Delivery reuses the exact injection path that `pending_prompt` / `rearm_background_tasks` already use
for system-originated prompts — no new write-to-session mechanism.

### 4. Loop guard — "one attempt per failure event"

In-memory `_pr_fix_attempts: dict[tuple[str, str], bool]` keyed by `(repo, branch)`, a sibling of the
existing `_pushed_pr` debounce set, updated inside `_on_pr_change`:

- On checks → `failing`:
  - if **not** already marked attempted → call `request_pr_fix`, mark attempted.
  - if **already** attempted → downgrade to notify-only (in `propose`, refresh/leave the proposal;
    in `auto`, just the push). **No second auto-attempt.**
- On checks → `passing` or state → `merged`/`closed` → clear the mark, re-arming a genuine future
  failure.

State is in-memory and resets on daemon restart — identical semantics to the existing `_pushed_pr`
debounce. Worst case after a deploy: at most one extra attempt on a still-failing PR. Acceptable and
consistent; noted here so it isn't mistaken for a bug.

### 5. Proposal surface (propose mode)

`propose` mode reuses the existing failing-PR surfacing rather than inventing a new one. The
`broken_pr_proposal_text()` helper in `server/sessions/dispatch.py` already produces broken-PR
suggestion text; the new piece is making it an **actionable** one-tap control on the failing-PR
session (card / PR pill) that calls `POST /api/sessions/{id}/fix-pr`. The action appears only when
the PR is actionable (open/draft, `anyChecksFailing` via `web/src/session/prStatus.ts`) and the mode
is `propose`.

## Data flow

```
PR poller (75s, GraphQL) detects checks: passing → failing
  └─ _on_pr_change(repo, branch, old, new)
       ├─ resolve owning session by (repo, branch)        [existing]
       ├─ push "PR broke" notification                     [existing, all modes]
       └─ mode gate:
            off      → done
            propose  → surface/refresh "Fix PR #N" action (loop-guard refreshes, doesn't re-push)
            auto     → loop guard:
                         first failure  → request_pr_fix → send_prompt
                                            ├─ live driver → inject (queues if busy)
                                            └─ no driver/draining → fall back to propose/notify
                         repeat failure → notify-only (no second attempt)

User taps "Fix PR #N"  → POST /api/sessions/{id}/fix-pr → request_pr_fix → send_prompt
```

## Error handling

- **No live driver / draining (auto):** fall back to notify/propose; do not resume the driver.
- **Session not found for (repo, branch):** notification only (current behavior); no fix.
- **Terminal PR (merged/closed):** never act — `request_pr_fix` and the proposal both gate on an
  actionable (open/draft) state.
- **send_prompt failure:** surfaced as the endpoint's response for the propose path; logged and
  downgraded to notify for the auto path.

## Testing

**Backend (`server/tests/`):**
- Settings round-trip for `prAutofixMode`: GET default (`propose`), PUT each valid value, invalid
  value ignored.
- `request_pr_fix` injects exactly once per failure event; second consecutive failure does not
  re-inject; re-arms after checks go green/merged.
- Mode gating: `off` injects nothing; `propose` surfaces the action but never auto-injects; `auto`
  injects.
- Busy owning session: fix prompt is queued (FIFO), the in-progress turn is not interrupted.
- No-driver/draining in auto mode: falls back to notify/propose, no driver resume.
- `POST /api/sessions/{id}/fix-pr` injects via the same path; auth required; gated on actionable PR.

**Frontend (`web/`):**
- SettingsPanel renders the new dropdown and persists the value (vitest + tsc).
- "Fix PR #N" action renders only in `propose` mode on an actionable failing PR and calls the
  endpoint; absent in `off`/`auto` and on terminal PRs.

## Open implementation notes

- Keep `request_pr_fix` and `build_pr_fix_prompt` as small, independently testable units (pure
  prompt builder + orchestrator method that wires guard → send_prompt).
- Reuse, don't duplicate: route the propose action through `broken_pr_proposal_text` /
  `anyChecksFailing` rather than adding parallel failing-PR logic.
