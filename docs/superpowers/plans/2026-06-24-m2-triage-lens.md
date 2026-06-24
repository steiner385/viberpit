# M2 — Triage Lens Implementation Plan (bite-sized)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax. Builds on **M1** (live-branch PR keying, PR #231) — branch M2 off a master that has M1 merged.

**Goal:** A single ranked "needs-me" attention queue across all sessions that fuses session status with PR state, so a session whose PR broke surfaces at the top even when the session itself is idle/done.

**Architecture:** Pure frontend extension of the existing single-source status-tier ranking (`web/src/session/statusTier.ts` → `TIER_RANK`, consumed by `ordering.ts` and `useNeedsYouTitle.ts`). No backend change — `session.pr` already rides the session-list payload (and after M1 is keyed on the live branch). Triage = "the more urgent of (status tier, PR-derived tier)".

**Tech stack:** React + TS + vitest. No new dependency.

## Global Constraints
- **Single source of truth (Constitution VI):** extend the existing `statusTier`/`TIER_RANK` vocabulary; do NOT add a parallel ranking. The `#215` trap (a hardcoded tier list silently dropping a tier) and `transient-status-persistence` memory apply — derive from `TIER_RANK`.
- **Read-only / auto (autonomy policy):** triage is a render-time computation over the polled session list; it updates automatically, no approval, no writes (AUT-01).
- **Frontend type gate is `tsc -b`** (never `tsc --noEmit`); eslint `--max-warnings 0`; vitest runs the FULL suite in CI (make any new optional field default-defensive).
- **No new poller / no backend change** for M2a.

Test-case IDs reference `docs/superpowers/specs/2026-06-23-combined-app-user-stories-and-test-cases.md`.

---

## Spike result (push path) — informs M2b

There is **no push/phone delivery path in the codebase today**: no web-push/VAPID, no service worker, no external sink (ntfy/Pushover/Twilio), no notify endpoints. The only attention surface is `useNeedsYouTitle.ts` (browser tab title "(N) ccswitch — needs you", requires the tab open). Therefore **M2b (top-tier push) is a NEW build with its own channel decision** (below) and is split out so it never blocks the in-app ranking (M2a), which ships standalone.

---

# M2a — In-app triage ranking (ships now)

**Files:**
- Create: `web/src/session/triageTier.ts`
- Modify: `web/src/session/ordering.ts` (status comparator + group-by-status use triage tier), `web/src/hooks/useNeedsYouTitle.ts` (count fused tier)
- Test: `web/src/session/__tests__/triageTier.test.ts` (new), extend `web/src/session/__tests__/ordering.test.ts` and `web/src/hooks/__tests__/useNeedsYouTitle.test.ts` if present.

**Interfaces:**
- Consumes: `statusTier(status) -> StatusTier`, `TIER_RANK`, `Session` (with `pr?: PullRequestInfo | null`, `pr.checks: 'passing'|'failing'|'pending'|'none'`).
- Produces: `triageTier(session: Session) -> StatusTier` — returns the more-urgent (lower `TIER_RANK`) of the session's status tier and a PR-derived tier; a PR with `checks === 'failing'` contributes tier `'error'`. Also `triageRank(session) -> number` = `TIER_RANK[triageTier(session)]`.

- [ ] **Step 1: Write the failing test** — `web/src/session/__tests__/triageTier.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { triageTier } from '../triageTier'
import type { Session } from '@/api/types'

const s = (status: string, checks?: 'passing'|'failing'|'pending'|'none'): Session =>
  ({ id: 'x', name: 'n', status, pr: checks ? ({ number: 1, url: 'u', state: 'open', checks, title: 't' } as any) : null } as Session)

describe('triageTier', () => {
  it('elevates a done session with a failing PR to error tier (AUT-03)', () => {
    expect(triageTier(s('done', 'failing'))).toBe('error')
  })
  it('leaves a done session with a green PR at your-turn', () => {
    expect(triageTier(s('done', 'passing'))).toBe('your-turn')
  })
  it('keeps needs-you above a failing PR (more urgent status wins)', () => {
    expect(triageTier(s('waiting_input', 'failing'))).toBe('needs-you')
  })
  it('no PR → falls back to status tier', () => {
    expect(triageTier(s('idle'))).toBe('your-turn')
  })
})
```

- [ ] **Step 2: Run it — expect FAIL** (module missing).
Run (web/): `npx vitest run src/session/__tests__/triageTier.test.ts`

- [ ] **Step 3: Implement `web/src/session/triageTier.ts`:**

```ts
import type { Session } from '@/api/types'
import { statusTier, TIER_RANK, type StatusTier } from './statusTier'

/** A broken PR is an attention signal even on an otherwise-calm session. */
function prTier(session: Session): StatusTier | null {
  return session.pr?.checks === 'failing' ? 'error' : null
}

/** Triage tier = the MORE URGENT (lower TIER_RANK) of the session's status tier
 *  and any PR-derived tier. Reuses the single TIER_RANK source — no parallel rank. */
export function triageTier(session: Session): StatusTier {
  const st = statusTier(session.status)
  const pt = prTier(session)
  if (pt && TIER_RANK[pt] < TIER_RANK[st]) return pt
  return st
}

export function triageRank(session: Session): number {
  return TIER_RANK[triageTier(session)]
}
```

- [ ] **Step 4: Run it — expect PASS.**

- [ ] **Step 5: Wire `ordering.ts` to triage tier.** Change the `status` comparator and the group-by-status tier assignment to use `triageTier(session)` instead of `statusTier(s.status)`. Keep deriving the tier list from `TIER_RANK` (don't fork). Update/extend `ordering.test.ts`: a `done`+failing-PR session sorts above an `idle`+green session and groups under "Needs attention" (error). Run the ordering tests.

- [ ] **Step 6: Wire `useNeedsYouTitle.ts` to count the fused tier.** Change the filter to count sessions whose `triageTier(s)` is `'needs-you'` OR `'error'` (a broken PR is "needs you" for the tab badge). Update `useNeedsYouTitle.test.ts`: a backgrounded session with a failing PR increments the title count. Run the hook tests.

- [ ] **Step 7: Full gates** (web/): `npx vitest run`, `npx tsc -b --force`, `npx eslint src/session/triageTier.ts src/session/ordering.ts src/hooks/useNeedsYouTitle.ts --max-warnings 0`.

- [ ] **Step 8: Commit** (`feat(triage): rank sessions by fused status+PR tier so broken-PR sessions surface`).

**M2a acceptance:** AUT-01 (auto, no approval), AUT-03 (done+failing-PR outranks idle+green); needs-you still top; tab title counts broken-PR sessions; no parallel ranking introduced; `tsc -b`/eslint/vitest green.

---

# M2b — Top-tier push (follow-on; NEEDS A CHANNEL DECISION)

**Status: blocked on a user decision — do not build until the channel is chosen.** The spike found no existing push path, so this is net-new infra. Options:

| Channel | Away-from-desk? | Cost to build | Notes |
|---|---|---|---|
| **External sink (ntfy / Pushover / generic webhook)** — *recommended* | ✅ real phone push | Low (server-side POST on transition; reuses Plotroom's proven webhook-notifier shape) | Simplest; matches "push/phone"; file-only URL per the security model (NFR-4) |
| Browser Web Notifications + service worker | ⚠️ only while browser process alive | Medium (SW + permission + subscription) | No phone delivery when browser closed |
| Web Push (VAPID) | ✅ | High (SW + VAPID keys + subscription store + server push) | Most infra for a single user |

**Recommended:** external webhook sink (ntfy topic or generic webhook), fired server-side on transition *into* the top tier (waiting-on-you / crashed / PR-broke), debounced once-per-condition (AUT-02). The URL is file-only config (never web-writable) per NFR-4. This mirrors the notification taxonomy ccswitch will eventually inherit from Plotroom, so it's not throwaway.

**When the channel is chosen, M2b expands to bite-sized:** (1) a pure `top_tier_transition(prev, now) -> bool` detector; (2) a debounced server-side sink firing the POST; (3) file-only URL config; tests AUT-02 (push only top tier; none for lower; once-per-condition; re-fire after clear).

---

## Self-Review
- **Spec coverage:** US-1/AUT-01/AUT-03 → M2a (Steps 1–8). US-3/AUT-02 (push) → M2b, correctly split out as blocked-on-decision (the spike proved it's net-new). ✓
- **Placeholder scan:** M2a has complete code/tests. M2b is explicitly decision-blocked, not a hidden TBD — the decision and options are stated. ✓
- **Type consistency:** `triageTier`/`triageRank` reuse `StatusTier`/`TIER_RANK` (no new vocabulary); `Session.pr.checks` matches the `PullRequestInfo` contract. ✓
