# Combined App — User Stories & Full Test-Case Catalog

**Date:** 2026-06-23
**Status:** Draft for review
**Source:** Operator user stories (founder persona) + test-case catalog (CI/CD persona).
**Reads with:** `2026-06-23-combined-app-requirements.md`, `2026-06-23-combined-app-architecture.md`.

Priority key: **P0** = MVP / ship-first · **P1** = soon after · **P2** = nice-to-have · **P3** = probably won't.
Test cases are Given/When/Then; pure-logic cases assume injected `now` and canned history rows.

---

## Part A — User stories (operator POV)

### Triage (JTBD: "tell me what needs me, across everything")
- **US-1 (P0)** As the operator, I want a single ranked "needs-me" list across all sessions, so that I stop manually scanning N cards. *AC:* top tier = waiting-on-input ∪ crashed ∪ this-session's-PR-broke; deterministic order; updates without refresh.
- **US-2 (P0)** As the operator, I want each session card to show its PR's live state, so that session and PR state live in one glance. *AC:* keyed on **live** worktree branch; "no PR" shows nothing, not an error.
- **US-3 (P1)** As the operator, I want a push **only** for top-tier items, so that away-from-desk I'm pinged for real things only. *AC:* once per condition per session; clears and can re-fire; no push for routine progress.
- **US-4 (P2)** As the operator, I want to dismiss/snooze a triage item, so that a known-ignored thing stops nagging. *AC:* snooze persists across refresh; re-surfaces if state worsens.

### Dispatch (JTBD: "feed an idle agent the next real task")
- **US-5 (P0)** As the operator, I want an idle/done session to show a chip proposing the next task, so that I don't hand-pick work. *AC:* sources = top issue (`prioritize-issues`) / broken PR (`gh-fix`) / review follow-up (`pr-review`); proposal only, never auto-launch.
- **US-6 (P0)** As the operator, I want to approve a suggestion with one tap and have the session launch pre-loaded with the right skill. *AC:* approve → starts on the issue's branch/skill; reject → next candidate.
- **US-7 (P1)** As the operator, I want the dispatcher to rank candidates (not just pick one), so that I can override. *AC:* top 1, expandable to top N; override remembered as signal.
- **US-8 (P2)** As the operator, I want the dispatcher to avoid proposing work already owned by another live session. *AC:* an issue with an open in-flight PR/branch is de-prioritized.

### Follow-through (JTBD: "react when a shipped PR breaks/stalls/ships")
- **US-9 (P0)** As the operator, I want a session whose PR broke in CI to surface that with a "fix it" chip. *AC:* on CI-failed for the session's live-branch PR, card shows break + `gh-fix` chip; approve → fix session launches.
- **US-10 (P1)** As the operator, I want a push when a PR stalls in the merge queue. *AC:* push on queue-blocked/stalled with culprit named; debounced.
- **US-11 (P1)** As the operator, I want a quiet (non-push) signal when a PR ships to prod. *AC:* badge/triage update on prod-live; no push by default.
- **US-12 (P2)** As the operator, I want overdue-stage and duration-regression signals. *AC:* surfaced in the pipeline view, not pushed.

### Unified pipeline view (enabler)
- **US-13 (P1)** As the operator, I want a Pipeline view inside ccswitch showing each PR's stage + ETA, so that I have Plotroom's view without a second app. *AC:* metro-row + expanded-check parity for the fields that matter; one poller.
- **US-14 (P1)** As the operator, I want learned ETAs accurate from day one after the merge. *AC:* `history.db` migrated/backfilled, not cold-started.
- **US-15 (P2)** As the operator, I want a queue-train strip and `behind N` position. *AC:* parity with Plotroom's queue strip.
- **US-16 (P3)** As the operator, I want the CI cost explorer. *Deferred/dropped (requirements §1).*

### Cross-cutting / operational
- **US-17 (P0)** As the operator, I want exactly **one** GitHub poller after the merge. *AC:* the engine absorbs `PrStatusPoller`; Plotroom's poller decommissioned in the cutover phase.
- **US-18 (P1)** As the operator, I want the migration to never leave me a *worse* tool mid-flight. *AC:* every phase ships a working unified app; no phase regresses an existing capability.
- **US-19 (P2)** As the operator, I want all writes (launch, push-fix, merge) to stay proposals until I explicitly promote a flow to auto. *AC:* read=auto; write=propose-and-wait.

### CI/CD-operator stories (pipeline-view lens)
- **US-20 (P0)** As an operator, I want the Python engine diffed against live Plotroom on demand, so that I can prove a port is faithful before trusting it. *AC:* the CLI prints matched/mismatch/oracle-only/candidate-only and exits 0/1/2; `--json` writes a report.
- **US-21 (P0)** As an operator, I want "Plotroom unreachable" to never masquerade as "outputs differ." *AC:* transport/non-200 → `HarnessError`, exit 2, parity counts untouched.
- **US-22 (P1)** As an operator, I want a phase to claim parity only on fields it produces. *AC:* coverage-guard test fails if a phase compares a field outside `PHASE_FIELDS[phase]`.
- **US-23 (P0)** As an operator, I want every write automation to propose, never act. *AC:* AUT-04/11/12 pass.
- **US-24 (P1)** As an operator, I want learned history migrated at estimator cutover. *AC:* release gated on a backfill-verification test.
- **US-25 (P1)** As an operator, I want a nightly parity-drift check on my real repos. *AC:* non-blocking nightly live-parity alerts when `ok` flips False on previously-green fields.
- **US-26 (P1)** As an operator, I want a flag to flip a ported capability off instantly. *AC:* default-off flag per capability; flip → fall back to prior path; capabilities < N unaffected.
- **US-27 (P0)** As an operator, I want the frontend type-checked the way CI checks it. *AC:* local verification uses `npx tsc -b --force` (never `tsc --noEmit`) + eslint `--max-warnings 0`.

---

## Part B — Full test-case catalog

### B.1 ETA estimator — conditional-remaining (`estimator/progress`)
- **EST-01** Given a running check with 0 samples; When ETA computed; Then no point ETA, not flagged overdue.
- **EST-02** Given 4 samples (<5); When computed; Then conditional-remaining does NOT engage; a simpler p50 estimate is used.
- **EST-03** Given exactly 5 samples and elapsed=30s; When computed; Then remaining re-anchors on the subset of samples exceeding 30s (not global p50).
- **EST-04** Given bimodal samples (~40s warm, ~180s cold) and elapsed=60s; When computed; Then estimate re-anchors on the cold cluster (>60s samples) — NOT a blended median predicting an already-passed finish.
- **EST-05** Given elapsed=200s and every sample <200s; When computed; Then `overdue` flips True.
- **EST-06** Given an overdue check re-run green with fresh start; When recomputed; Then `overdue` False.
- **EST-07** Given 14 samples; Then p50/p90 match observed `check_durations` within tolerance, not a hardcoded default.
- **EST-08** Given 14 first-ETA predictions vs actuals; Then footer shows median absolute error as `typically ±Xm (n=14)`; sub-minute renders `±45s`.
- **EST-09** Given a stage re-estimated 6× in one run; Then it contributes ONE accuracy sample (first-ETA vs actual), not 6 — `n` counts runs.
- **EST-10** Given Python ETA=122s, Plotroom=119s; When harness compares; Then matched within `±max(2s,5%)`.
- **EST-11** Given two computations at elapsed 30s then 60s, unchanged history; Then remaining(60s) ≤ remaining(30s).

### B.2 Stage machine + parked substates (`pipeline-model`)
- **STG-01** required checks green + entering queue → stage=`merge-queue`, CI segment done.
- **STG-02** merged PR for a repo with a `qa` env → stage=`qa-deploy`.
- **STG-03** merged PR for a repo with no `deploy.<repo>` → terminates at merge (no QA/prod nodes).
- **STG-04** draft PR → `parked/draft` (and `is_draft` parity field True).
- **STG-05** conflicting PR → `parked/conflicting`.
- **STG-06** required check failed → `parked/ci-failed` (triggers NTF-01).
- **STG-07** queued PR's merge-group build fails → `parked/queue-group-failed` (triggers NTF-02).
- **STG-08** checks go green → `ready/armed` vs `ready/idle` correctly distinguished (drives NTF-04).
- **STG-09** queued PR with 3 ahead → `behind 3`.
- **STG-10** `batchSize=6`, 13 queued → per-repo override wins over global; groups into batches of 6.
- **STG-11** Plotroom `state=OPEN, isDraft=true` → both candidate and oracle normalize to `draft` (isDraft wins).

### B.3 Runner pickup-wait — waiting vs blocked (`estimator/waits`)
- **RNR-01** every `needs:` dep completed OK → `waiting for runner` (eligible).
- **RNR-02** a named upstream not complete → `blocked on <job>`, not "waiting for runner".
- **RNR-03** wait=3m, typical=1m → amber (>2× typical).
- **RNR-04** wait=1.5m, typical=1m → not amber.
- **RNR-05** wait learned as `startedAt − max(needs' completedAt)`, keyed per `(repo,check,event)`, with no extra API calls.
- **RNR-06** still-queued eligible job + learned median → median added to remaining estimate.
- **RNR-07** `startedAt` ≤ `max(needs completedAt)` (skew) → wait clamps ≥ 0 (no negative poisoning).

### B.4 Phase-aware needs-graph (`derive-static` / `gating`)
- **NDS-01** a `merge_group`-only job as a `needs:` of a PR-phase check → satisfied by absence, not blocking forever.
- **NDS-02** a same-phase incomplete upstream → correctly blocks (NDS-01 must not over-suppress).
- **NDS-03** `ci.yml` with `rollupJobId: ci` → each job in `ci`'s `needs:` closure contributes a required prefix.
- **NDS-04** a check named like a required prefix but emitted by a helper workflow (`Auto-merge PRs`) → excluded from the required set.
- **NDS-05** `requiredCheckPrefixes: []` → prefix matching fully disabled for that repo.
- **NDS-06** derivation not yet run → fallback to `['ci']`.
- **NDS-07** `ci.yml` unparseable → previous prefixes retained, no crash.
- **NDS-08** `workflowPath` no longer resolves, no explicit pin → rollup job's workflow auto-discovered and adopted.
- **NDS-09** `aliases: {static-checks: checks}` → learned history rewritten under `checks` (idempotent, reversible) so ETAs don't cold-start.

### B.5 Deploy ancestry — api vs clone (`ancestry`)
- **ANC-01** api mode, merge commit is compare-API ancestor of deployed SHA → "live on prod" True; no clone created.
- **ANC-02** api mode, not an ancestor → shipped False (no premature prod-live).
- **ANC-03** clone mode for the same repo/commit → result equals api-mode result.
- **ANC-04** api mode full sweep → `data/clones/` not populated.
- **ANC-05** api transport error + pre-existing clone → clone serves as transport-error fallback only.
- **ANC-06** `healthUrl` JSON with custom `shaKey` → configured key honored (default `commitSha`).
- **ANC-07** `qa.auto=true`, `prod.auto=false` → QA transitions automatically, prod waits.

### B.6 Notification taxonomy + debounce (`notifier`/`digest`)
- **NTF-01** PR enters `parked/ci-failed` → one `ci-failed`; a second poll while still failing fires nothing.
- **NTF-02** queued PR's merge-group fails → `group-failed`.
- **NTF-03** queue entry UNMERGEABLE via cascade → `queue-blocked` with `detail` naming the culprit PR.
- **NTF-04** checks green with `ready` at default (off) → no `ready` notification.
- **NTF-05** ci-failed fired → retried green (clears) → fails again → fires a SECOND time.
- **NTF-06** merged PR becomes prod ancestry → `prod-live` exactly once per PR per process lifetime.
- **NTF-07** repo merge queue dispatch-stalls → `queue-stalled` once per repo (not per PR).
- **NTF-08** recent 10-run median ≥ 1.5× prior 20-run median AND +60s → `duration-regression`; clears only below ratio 1.2 (asymmetric hysteresis).
- **NTF-09** pool last-hour pickup-wait p90 > max(5min, 4× 7-day baseline p90) with ≥5 samples → `runner-starvation`; clears below 2× baseline.
- **NTF-10** same breach but only 4 samples → does NOT fire.
- **NTF-11** `events: {ci-failed: false}` → no sink (command/webhook/browser) fires for ci-failed.
- **NTF-12** `webhookUrl` set + failing receiver → one POST, 5s timeout, no retry, logged ≤1×/hr, poll never crashes.
- **NTF-13** hostile PR title with shell metachars → `execFile` argv substitution, no injection.
- **NTF-14** digest at `hourLocal:8` across a DST boundary → re-arms to next wall-clock 08:00; compose failure doesn't kill schedule. *(digest is dropped per requirements; test retained only if digest is ever ported.)*
- **NTF-15** `PUT /api/config` carrying `notifications.command` → rejected (file-only).
- **NTF-16** token-bearing `webhookUrl` → only scheme+host shown in `GET /api/config`, settings panel, and logs.

### B.7 The parity harness itself (`parity/`)
- **PAR-01** identical candidate/oracle → matched=1, no mismatches, `ok` True.
- **PAR-02** candidate `state=open` vs oracle `state=merged` → `FieldMismatch(field="state")`, `ok` False.
- **PAR-03** `title` difference (not in `PHASE_FIELDS[0]`) → `ok` stays True (anti-noise guarantee).
- **PAR-04** oracle has `(o/r,2)` absent from candidate → in `oracle_only`, `ok` False.
- **PAR-05** candidate has `(o/r,1)` absent from oracle → in `candidate_only`, `ok` False.
- **PAR-06** oracle row lacks a covered field entirely → field skipped for that row, not a mismatch.
- **PAR-07** `covered_fields(99)` → `KeyError`.
- **PAR-08** `covered_fields(0)` → excludes `eta_secs`/`stage`.
- **PAR-09** Plotroom HTTP 500 / refused → `fetch_oracle` raises `HarnessError`, CLI exit 2, error recorded; not conflated with mismatch.
- **PAR-10** parity holds → exit 0; mismatch → 1; harness error → 2.
- **PAR-11** Plotroom's "most relevant PR" per branch differs from ccswitch's open/draft>merged>most-recent-closed rule → flagged as a SELECTION mismatch, distinct from a field mismatch.
- **PAR-12** timestamps differing only sub-second → matched at second granularity (`TIMESTAMP_GRANULARITY_SECS=1`).
- **PAR-13** recorded `plotroom_state_sample.json` → normalized, keyed `(repo, number)`, canonical field names; if fixture and adapter disagree, fix the adapter, not the assertion.
- **PAR-14** same checks in different order → matched (harness sorts `(name, conclusion)` tuples).
- **PAR-15** an ETA field added to a phase → comparison uses `±max(2s,5%)`, asserted, never `==`.

### B.8 Branch → session join (live branch, not stale)
- **JOIN-01** stored branch `ccswitch/master-x` but HEAD now `feat/foo` → join keys on `worktrees.live_branch` (`feat/foo`); badge/pipeline appears.
- **JOIN-02** session with null branch → no snapshot; excluded from comparison and join.
- **JOIN-03** completion workflow moves worktree to a fresh PR branch mid-session → re-poll re-resolves to the new live branch (no stale frozen badge).
- **JOIN-04** two sessions on two branches each with a PR → each card joins its own PR (no cross-talk).
- **JOIN-05** after the port absorbed `PrStatusPoller` → exactly one PR poller running (no parallel old+new).

### B.9 The three automations + mixed-by-risk gate
**Triage Lens (read-only → auto):**
- **AUT-01** session status fused with pipeline state → attention queue updates with NO approval.
- **AUT-02** session waiting-on-user OR crashed → push fires; lower-tier items → no push.
- **AUT-03** a `done` session whose PR is `parked/ci-failed` → ranks above a merely-idle session with a green PR.

**Work Dispatcher (write → propose):**
- **AUT-04** session goes idle/done → a chip proposes the next task; NO session launches without approval.
- **AUT-05** chip source ∈ {top issue, flagged broken PR, review follow-up}; never a hand-curated backlog.
- **AUT-06** approve → session launches pre-loaded with the matching skill.
- **AUT-07** dismiss → no session starts, no write occurs.
- **AUT-08** proposal renders through the existing `latest_suggestions`+ghost-text+Resume/chevron surface, not a reimplementation.

**PR Follow-through (mixed):**
- **AUT-09** pipeline event routes to the owning session by live branch → badge/triage updates automatically.
- **AUT-10** PR breaks/stalls/ships → push fires (alert-worthy transitions).
- **AUT-11** PR breaks → a fix-proposal chip appears; it does NOT auto-push a fix; approve → fix session launches.
- **AUT-12** any flow that would merge → only proposes; merge never auto-executes.
- **AUT-13** event routes via `worktrees.live_branch` (composes JOIN-01); a stale branch must not misroute or drop the event.

*AUT-04/11/12 encode the autonomy policy as testable, release-blocking invariants — never skip/xfail to land a feature.*

### B.10 Cost explorer (only if un-dropped; retained for completeness)
- **CST-01** jobs with `runs-on` pools + rates → occupancy (start→end, all conclusions incl. failed/cancelled) attributes to the pool.
- **CST-02** both rates set for a label → `poolMeta.<pool>.dollarsPerMinute` wins.
- **CST-03** `podsPerNode:4` → effective rate = rate ÷ 4.
- **CST-04** unpriced pool, no `default` → minutes reported, excluded from $ totals (documented undercount, never a fabricated zero).
- **CST-05** two POSTs to `/api/cost/actuals` for same `(scope,date)` → row upserts (idempotent).
- **CST-06** array with one invalid `dollars` → whole import rejected (no half-import).
- **CST-07** browser cross-site POST to `/api/cost/actuals` → blocked; header-less curl/cron passes.

### B.11 Test levels (taxonomy)
1. **Pure-function unit** — estimator/stage math, canned history, injected `now`. Backbone.
2. **Integration** — poller→snapshot→`list_sessions`; estimator over the real SQLite schema; durable-temp-file shim for `:memory:`; `init_db` nullable backfill.
3. **Parity** — golden-fixture (CI, offline) + live (operator-run acceptance, Constitution III).
4. **Contract** — daemon↔edge RPC round-trips; frontend↔API payload stability.
5. **Golden-fixture** — recorded `/api/state` (oracle) + recorded GitHub responses (candidate input).
6. **End-to-end** — PR CI→queue→QA→prod → engine stage → notification → live frame → metro row → chip on the owning session.
7. **Web component/interaction (vitest)** — Pipeline view render, status-strip filter, Gantt expand, queue-train anchor-scroll. Beware the full-vitest CI trap: make new required type fields default-defensive.
