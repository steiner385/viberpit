# Refresh-failure instrumentation & recovery — design

**Date:** 2026-06-25
**Status:** Approved (design); pending implementation plan
**Branch:** `fix/refresh-failure-instrumentation-and-recovery`

## Problem

On 2026-06-25 at 11:23 UTC, all three accounts (kindash, primary, secondary) flipped to
`needs-reauth` within one second, on `token refresh failed with 400`. They did **not**
self-recover; the user manually re-authenticated all three (audit `account.reauth.success`
11:25–11:29 UTC). This is the latest recurrence of a long-running bug class — prior fixes
#54bfd71, #195, #240, #145, #165, #9, #652d0c7 each patched one HTTP status or one race, but
never closed the underlying design fault.

### Confirmed evidence
- **Trigger:** the usage poller's proactive idle-token refresh. Idle accounts (no live session)
  are polled with `allow_refresh=True` (`orchestrator.py:215`), so the poller itself refreshes
  them. The three 400s at 11:23 + a repeat at 11:24 (next poll cycle) is the poller, not a
  dashboard read or a launch.
- **Precondition:** ~13 hours of sustained `usage endpoint returned 429` across all accounts
  (22:00 UTC 06-24 → 11:23 UTC 06-25) immediately preceding the flip.
- **Not a misclassification of a transient blip:** the 400 stayed terminal for 14 minutes until
  a human logged in. A throttle-induced transient would have cleared on retry. The refresh token
  ccswitch held was genuinely rejected.
- **Architecture:** ccswitch keeps its own per-account credential copies in
  `store/<name>.cred.json` (the "slot"), separate from the live `cfg/<name>/.credentials.json`
  the `claude` SDK reads and rotates. Both refresh the **same single-use, rotating** grant.
  `sync_back` only copies live → slot, and only when it runs.

### Root cause (mechanism)
Two code-level hazards, both amplified by the 429 storm:

1. **Non-idempotent refresh retry.** `oauth.http_request` retries `_is_transient_status` =
   `{403, 429, 5xx}` and network timeouts by **resending the same body** — i.e. the same
   single-use refresh token. A refresh whose request reaches the server (token rotated
   server-side) but whose response is lost/throttled is retried with the now-consumed token →
   genuine `invalid_grant`. The new token was minted but ccswitch never received it; its slot
   copy is permanently orphaned. A 13h throttling storm makes this likely across all accounts at
   once.
2. **`needs-reauth` is terminal.** Nothing re-derives a slot token from the still-valid login
   family or the live `cfg/` dir; it waits for a human. This is why it never self-recovered.

### The instrumentation gap
`audit_events` records the *recovery* (`account.reauth.start/success/cancel`) but has **no event
for the failure transition**. The flip exists only as a transient `log.warning("...400")` with no
response body, call-path, or precondition context. Every recurrence — including this one — is
therefore unprovable after the fact: the body that would decide `invalid_grant` vs other-400 is
discarded at `oauth.py`.

## Goals
1. Make the next occurrence **conclusive** — durably capture the failure with cause attached.
2. Make `needs-reauth` **recoverable** without human intervention when the grant is still alive.
3. **Stop ccswitch from orphaning its own token** via non-idempotent retry.

Non-goals (documented follow-ups, see below): the poller 429 circuit breaker; the
`merge_keep_later` live-handoff guard.

## Design

Layering rule: the **core** (`src/ccswitch`) stays DB-free; the **server** owns audit
persistence. The core surfaces structured failure data; the server persists it.

### Component 1 — Core: capture the failure (`oauth.py`, `creds.py`)
- `refresh_access_token`: on the terminal 400/401, attach the truncated, **un-redacted-at-source**
  response body and status to a structured `AuthError(status=…, body=…)` (today: bare
  `"token refresh failed with {status}"`). Redaction happens at the server audit boundary, which
  already strips token material (`server/audit.py`).
- **Idempotency fix:** add `idempotent=False` to the token POST. When set, `http_request` retries
  only failures that provably did not reach the server (429 = edge-rejected pre-processing; and
  pre-send connection errors) and does **not** resend on after-send ambiguity (read-timeout / 5xx).
  Usage GET stays `idempotent=True` (unchanged).

### Component 2 — Core: recoverable token acquisition (`accounts.py`)
- New `acquire_valid_token(name)`: try the slot; if a refresh raises `AuthError`, run
  `sync_back(name)` to pull the live `cfg/` token and, if a **fresher valid** token appeared, use
  it. Only when no known copy yields a working token does the caller flip `needs-reauth`.
- `usage_for`'s `allow_refresh=True` path routes through `acquire_valid_token`; its `AuthError`
  handler (`accounts.py:147`) only sets `needs-reauth` after recovery is exhausted.
- For a truly idle account whose live `cfg/` holds the same stale token, recovery is a no-op and
  the flip proceeds correctly.

### Component 3 — Server: durable failure event (`server/`)
- New audit type `account.needs_reauth`. Detail (run through existing `server/audit.py`
  redaction): `{account, status, body, path, token_expiresAt, attempts}` where
  `path` ∈ {`poller-idle`, `live-session`, `cli`, `dashboard`}. (`recent_usage_429s` deferred to
  the circuit-breaker follow-up, which owns throttle tracking.)
- **Seam:** core exposes `accounts.set_auth_failure_sink(fn)`; the server registers a
  `record_audit`-backed sink at startup. The core calls the sink at **both** flip sites
  (`usage_for` and `orchestrator._mark_needs_reauth`) so emission has one source of truth. An
  unset sink (CLI usage) is a no-op plus the existing log line — the core never imports the DB.
- `recent_usage_429s`: a small rolling per-account counter the poller already has the data for,
  so the audit captures the throttling precondition.

### Component 4 — Tests (TDD, written first)
- `oauth`: 400 attaches body to `AuthError`; an after-send failure (read-timeout / 5xx) on a
  refresh with `idempotent=False` does **not** resend; a 429 still retries.
- `accounts`: `acquire_valid_token` recovers when live `cfg/` is fresher; flips `needs-reauth`
  only when no copy works.
- `server`: `account.needs_reauth` audit row is emitted with the body but **zero** token material
  (extend `test_reauth_no_token_leakage`).

## Error handling
- Sink failures must never break auth: the sink call is wrapped and swallowed (the flip + log
  still happen). Mirrors `orchestrator._mark_needs_reauth`'s existing try/except.
- Body capture is size-bounded (truncate to 2KB) to avoid logging large Cloudflare HTML.
- Redaction is enforced at the server boundary and asserted by tests; the core never decides
  redaction.
- The core's existing `log.warning` line stays body-free (status + path only). The captured body
  travels **only** on the structured `AuthError`/sink and is redacted at the server boundary
  before it reaches `audit_events`. This keeps the no-token-leakage guarantees (`log` args carry
  no body) intact even though a 400 error body never contains token material.

## Risks
- Auth control-flow change (blast radius). Mitigated by TDD, the no-token-leakage tests, and
  keeping CLI behavior identical when the sink is unset.
- `acquire_valid_token` adds a `sync_back` + retry on the failure path only — no extra work on the
  happy path.

## Deferred follow-ups (out of scope, tracked separately)
1. **Body-based 400 classification** — flip `needs-reauth` on a 400 ONLY when the body is a
   definitive `invalid_grant` (vs. a Cloudflare HTML 400 or a rate-limit 400, which should be
   transient). Deliberately deferred until Component 1 captures a **real** failing-400 body:
   writing the classifier against an assumed body format would violate constitution III (verify
   against the real binary/API, not assumptions). This is the highest-value *next* fix and the
   instrumentation here is precisely what unblocks it.
2. **Poller 429 circuit breaker** — back off per-account when usage 429s streak, so a throttling
   storm can't run for 13 hours and manufacture the dangerous refresh window. Precondition, not
   the auth defect; independently testable. Owns the `recent_usage_429s` throttle tracking.
2. **`merge_keep_later` guard** (`creds.py:70`) — it keys on `expiresAt`, assuming "later
   access-token expiry = newer refresh token." With two refreshers on one grant that proxy can
   keep a stale slot blob and discard the truly-current refresh token. Latent on the live-handoff
   path; not this idle incident.
