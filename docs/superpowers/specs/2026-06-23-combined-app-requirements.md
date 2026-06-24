# Combined App (ccswitch ⨉ Plotroom) — Requirements Specification

**Date:** 2026-06-23
**Status:** Draft for review
**Method:** Synthesized from three persona analyses — Software Architect, CI/CD & Quality specialist, and a part-time solo-dev/startup founder.
**Companion docs:**
- Architecture → `2026-06-23-combined-app-architecture.md`
- User stories + full test catalog → `2026-06-23-combined-app-user-stories-and-test-cases.md`
- Prioritized roadmap, MVP, metrics, delivery risks → `2026-06-23-combined-app-roadmap.md`
- Program context → `2026-06-23-ccswitch-plotroom-merge-overview-design.md` (the original program design this spec refines)

---

## 0. HEADLINE DECISION FOR THE USER: sequencing

All three personas independently converged on one tension, and it is the most important thing in this spec:

**The three automations — Triage Lens, Work Dispatcher, PR Follow-through — are the product. The Level-3 merge is plumbing. The committed program (overview design) ships the plumbing first (Phases 0–4) and the product last (Phases 5–7).**

The founder persona argues this is backwards and the highest *delivery* risk in the program: for a 5–10 hr/week solo developer, gating the payoff behind months of ETA-parity porting and a cost-explorer port risks stalling at *"I successfully rebuilt Plotroom in Python"* having shipped zero automations.

The architect and CI/CD personas corroborate the enabling fact: **the automations depend on two primitives that already exist** — the live-branch→session join (`worktrees.live_branch`, PR #221) and the existing suggestion-chip surface (`latest_suggestions` + ghost text + Resume/chevron) — **not on the ported Python pipeline engine.** Their inputs (session status, "is this PR open/failing/merged", open issues, review comments) are already available from ccswitch's current `pr_status.py`/`pr_poller.py` + `gh`.

**Two requirement sets follow from this, and the user must pick the sequencing:**

- **Option A — Value-first (recommended by the personas):** keep Level 3 as the *destination* but do NOT gate automations behind it. Ship Triage + Dispatch + PR-broke-fix on the *current* PR data with Plotroom left running (frozen) as a separate tab; then do the strangler port to fold Plotroom in and unlock richer stall/ship signals. First felt value in ~3–4 weeks.
- **Option B — Foundation-first (the committed overview order):** Phase 0 → 4 port and decommission Plotroom, then Phases 5–7 automations. Cleaner single codebase sooner; payoff later; higher abandonment risk.

This spec is written to be **sequencing-agnostic for requirements** (the same requirements hold either way) and flags sequencing only in the roadmap. The roadmap doc presents Option A as the default recommendation. **Confirm A or B before the roadmap is treated as committed.**

---

## 1. Scope

### In scope
- A single application: ccswitch's Python/FastAPI backend + unified React frontend, with Plotroom's PR/CI pipeline capability folded in (true merge, Level 3, target = Python because the `claude-agent-sdk` pins it there).
- Three automations closing the loop between running Claude sessions and the PRs they ship:
  - **Triage Lens** — a ranked "what needs me" attention queue across all sessions.
  - **Work Dispatcher** — propose the next GitHub task to an idle/done session.
  - **PR Follow-through** — react when a session's PR breaks / stalls / ships.
- Autonomy policy: **mixed by risk** — read/observe flows act automatically; every write (start a session, push a fix, merge) only *proposes* and waits for approval.

### Explicitly OUT of scope (dropped, not deferred — founder persona, justified for a single user)
- **CI cost explorer + actuals import + pool-rate relay** — largest surface with zero overlap with the automations; biggest hours-sink. A user who wants it keeps a standalone Plotroom.
- **Kiosk mode** — wall display for a one-person fleet.
- **GitHub App mode** — the `gh` CLI token already authenticates ccswitch; App registration/JWT/multi-installation is enterprise-shaped for one account.
- **Webhook receiver** — polling is primary and adequate; webhooks need a tunnel + HMAC for marginal latency.
- **Daily digest** — explicitly not selected in brainstorming (push + chips were).
- **Settings panel hot-config** — ccswitch has its own config story; do not port a parallel one (Constitution VI).

### Deferred (port only when its consumer lands)
- Pipeline stage machine + ETA engine + learned history (port when retiring Plotroom).
- Deploy ancestry (port when wiring prod-live).
- SSE frame-dedup stream — **candidate to drop, not port**: ccswitch already has a live WebSocket channel; reuse it rather than re-port SSE.
- In-repo `.pr-dashboard.yml` config layer (port only if a deploy-stage need forces it).

---

## 2. Functional requirements

### FR-1 Unified PR/session state
- **FR-1.1** Every repo-backed session SHALL display its PR's live state (open/draft/merged/closed + CI passing/failing/pending) on its card, keyed on the **live** worktree branch (`worktrees.live_branch`), never the stale stored creation branch.
- **FR-1.2** Sessions with no PR SHALL show no PR indicator (absence is the signal); a detached worktree (null branch) is never queried.
- **FR-1.3** There SHALL be exactly **one** GitHub PR poller after the merge; the ported engine consumes the existing `PrStatusPoller` sweep, never a second loop (Constitution VI).

### FR-2 Triage Lens (read-only → automatic)
- **FR-2.1** The system SHALL compute a single ranked "needs-me" queue across all sessions, fusing session status with PR/pipeline state, updated without manual refresh.
- **FR-2.2** Ranking top tier = (session waiting on input) ∪ (session crashed) ∪ (this session's PR broke). It SHALL outrank merely-idle sessions with healthy PRs.
- **FR-2.3** Triage SHALL act automatically with no approval (read flow).
- **FR-2.4** A push/phone alert SHALL fire **only** for top-tier items; lower tiers update silently.

### FR-3 Work Dispatcher (write → propose)
- **FR-3.1** When a session goes idle/done, the system SHALL surface a suggestion chip proposing the next task; it SHALL NOT launch a session without explicit approval.
- **FR-3.2** Candidate task sources SHALL be: top open GitHub issue (`prioritize-issues`/`implement-loop`), a flagged broken/blocked PR (`gh-fix`), or a review follow-up (`pr-review`). A hand-curated backlog is NOT a source (rejected in brainstorming).
- **FR-3.3** The chip SHALL show *why* a task was picked (priority/label, or "CI red since HH:MM").
- **FR-3.4** Approval SHALL launch the session pre-loaded with the matching skill and target; dismissal SHALL leave no side effect and offer the next candidate.
- **FR-3.5** The chip SHALL reuse the existing `latest_suggestions` + ghost-text + Resume/chevron surface — not a parallel implementation.

### FR-4 PR Follow-through (mixed)
- **FR-4.1** Pipeline events SHALL route back to the owning session by live branch; badge/triage updates are automatic (read).
- **FR-4.2** A push SHALL fire when a tracked PR breaks (CI-failed), stalls (queue-blocked/stalled, culprit named), or ships (prod-live). prod-live is quiet (badge/triage) per the top-tier push policy unless promoted.
- **FR-4.3** A broken PR SHALL produce a `gh-fix` proposal chip on its owning session; it SHALL NOT auto-push a fix. Merge SHALL never auto-execute.

### FR-5 Pipeline view (when the engine is ported)
- **FR-5.1** A Pipeline view inside the unified app SHALL show each open PR's stage (CI → merge-queue → QA → prod) with ETA, matching Plotroom's metro-row + expanded-check semantics for the fields that matter.
- **FR-5.2** Learned ETAs SHALL be accurate from day one after cutover (history migrated, not cold-started).
- **FR-5.3** State vocabulary SHALL map onto ccswitch's existing `statusTier`/`TIER_RANK` tokens, not a second status palette.

### FR-6 Autonomy gate (cross-cutting, testable invariant)
- **FR-6.1** Read/observe flows act automatically; every write (launch session, push fix, merge) only proposes and waits.
- **FR-6.2** The read-auto / write-propose distinction is a **release-blocking correctness invariant**, not a UI preference; its tests SHALL never be skipped/xfail'd to land a feature.
- **FR-6.3** Promotion of a specific write flow to auto SHALL be an explicit user action, never a default.

---

## 3. Non-functional requirements (architect)

- **NFR-1 Performance/freshness:** preserve tiered polling — ~60s baseline sweep, ~15s hot when PRs are in flight, ~30s deploy-health. ETA freshness bounded by sweep interval. Pickup-wait learning stays derived from already-fetched data (no per-job API fan-out).
- **NFR-2 Rate-limit safety:** honor a `rateLimitFloor` (degrade to slow intervals below it); one shared `gh` token via `gitrepos._run_gh`; the engine adds no uncached per-PR GitHub calls.
- **NFR-3 Reliability / graceful degradation:** PR/pipeline data is decorative and SHALL never raise into a dashboard request; if the engine lags or history is sparse, degrade to "stage known, ETA unknown" (ETA `None`/conditional until ≥5 samples). SSE/live channel auto-reconnects; the daemon graceful-drain handshake (PR #190) SHALL NOT drop the poller mid-deploy.
- **NFR-4 Security boundaries:** loopback-only / Tailscale-gated; same-origin CSRF guard on mutations. Credential/network/host-command config (`tokenSource`, `apiUrl`, `port`, app creds, notification command + webhook URL) SHALL remain **file-only** (rejected by any web config-write path). Host-command notifications SHALL run via `execFile` (argv array, never a shell). Token-bearing webhook URLs SHALL be host-masked everywhere.
- **NFR-5 Concurrency/single-writer:** only the daemon writes SQLite (both the main DB and any engine DB); the edge reads via RPC, never opening DB files directly. No blocking sqlite handle in an async edge request path.
- **NFR-6 Resource limits:** honor history retention pruning; add no new long-lived processes (engine runs in-task), preserving the `TasksMax` lesson (PR #152 incident).
- **NFR-7 RPC round-trip:** every new type crossing the daemon↔edge boundary (e.g. stage/ETA snapshots) SHALL implement `to_dict`/`from_dict` and have a round-trip test (the documented under-tested seam).

## 4. Quality / CI-CD requirements (CI/CD specialist)

- **QR-1 Parity harness is a first-class gate.** No Plotroom capability may be decommissioned until the harness shows parity over that capability's `PHASE_FIELDS` against **live** Plotroom for real repos. Never loosen the coverage set to pass; a mismatch is an adapter bug or a real port finding — both are fixed, not hidden.
- **QR-2 TDD mandatory** for all ported logic (Constitution V): RED → minimal impl → GREEN → commit. The estimator/stage-machine ports are the least permitted to skip a RED-first test.
- **QR-3 Lint/type gates with the documented traps:** backend ruff + mypy + pytest (CI fails fast on lint); frontend vitest + **`tsc -b`** (NEVER `tsc --noEmit`, which checks nothing) + eslint `--max-warnings 0`.
- **QR-4 Risk-tiered coverage** (not a flat %): ~100% branch coverage + a golden-fixture parity test for pure ported heuristics and the harness itself; error+happy paths for adapters; render+interaction for the Pipeline view; full write-gate coverage for the automations.
- **QR-5 Two toolchains during transition** are accepted; the Node/Plotroom toolchain stays in its own repo until decommission and is never merged into ccswitch CI.
- **QR-6 Parity as a CI gate runs offline** (golden fixtures), because live Plotroom is loopback-only and unreachable from CI runners; live parity is an operator-run per-phase acceptance gate with committed evidence blobs (`docs/superpowers/evidence/<date>-parity-phaseN.json`).
- **QR-7 Cutover PR gating:** a phase's decommission PR requires both required checks green + golden-fixture parity green + a live-parity evidence blob (`ok: true`) attached + the old Plotroom path deleted in the same PR.

## 5. Release / rollout requirements

- **RR-1** Each ported capability sits behind a default-off flag; the Python engine and live Plotroom run in parallel during the parity window (shadow → canary → cutover → decommission).
- **RR-2 Rollback = flag flip** (fall back to the prior path while Plotroom still runs pre-decommission); post-decommission rollback = revert the cutover commit (hence the old path is deleted in the cutover PR for a clean inverse).
- **RR-3 History continuity is a release gate:** the estimator-cutover release SHALL be gated on a backfill-verification test (post-migration ETAs match pre-migration Plotroom within tolerance).
- **RR-4** Honor ccswitch deploy norms: daemon graceful-drain, protected `master` + auto-merge, `allow_update_branch` auto-rebase, `Fixes #N` to close the phase's tracking issue.

## 6. Observability requirements

- **OR-1** Reuse the notification taxonomy as the ops signal (no parallel alerting): `queue-stalled`, `runner-starvation`, `duration-regression`, `overdue` wired to the operator push surface at top tier only.
- **OR-2** Port the connection/staleness badge (`live · updated HH:MM` / `disconnected — retrying…` / `stale since HH:MM`).
- **OR-3 Parity-drift alarm:** a non-blocking nightly live-parity run alerts when `ok` flips False on previously-green fields (early warning of a Plotroom-vs-Python divergence in prod); "Plotroom unreachable" is paged differently from "outputs differ".

## 7. Governance / constitution touchpoints

- **Principle III (verify against the real thing):** the parity harness is this principle turned into a test rig.
- **Principle VI (single source of truth / no dead code):** one poller; reconcile required-check-prefix logic into one Python impl (don't fork Plotroom's + ccswitch's); map status colors onto existing tokens; delete each Plotroom path in its cutover PR; `normalize.py` is the single encoding of Plotroom's `/api/state` shape and dies in the decommission phase.
- **Principle IV (security-first, exposure-conditional):** preserve file-only credential config and `execFile` notification safety on the port.
- **Principle V (TDD + quality gates):** QR-1..QR-7.

## 8. Open decisions requiring user input

1. **Sequencing — Option A (value-first) vs Option B (foundation-first).** See §0. *Recommended: A.*
2. **SSE: port or drop** in favor of ccswitch's existing WebSocket live channel (architect ADR-4 vs founder "candidate to drop"). *Lean: reuse the existing channel; port SSE only if frame-dedup proves necessary.*
3. **Notification delivery:** keep Plotroom's tuned event taxonomy + debounce, but deliver via ccswitch push/phone + chip rather than the browser-bell/host-command stack? *Recommended: taxonomy yes, parallel sinks no.*
4. **Engine history store:** separate `pipeline_history.db` vs folding into the main SQLite (architect ADR-1). *Recommended: separate DB.*
