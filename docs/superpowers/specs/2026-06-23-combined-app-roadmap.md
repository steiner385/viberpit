# Combined App — Roadmap, Prioritization & Delivery Plan

**Date:** 2026-06-23
**Status:** Draft for review — **contains a recommended revision to the committed phase order; needs user sign-off (requirements §0).**
**Source:** Founder persona (scope/sequencing/metrics), reconciled with architect + CI/CD.

---

## 1. The central recommendation: value-first sequencing

The committed program (overview design) order is: **Phase 0 parity harness → 1 stage+ETA port → 2 ancestry/stream/notifications → 3 frontend merge → 4 cost+decommission → 5 Triage → 6 Dispatch → 7 Follow-through.** That ships the *plumbing* first and the *automations you actually want* last.

**Recommended revision (Option A):** keep Level 3 as the **destination**, but do **not** gate the automations behind it. The automations depend on primitives that already exist — the live-branch→session join and the existing suggestion-chip surface — **not** on the ported Python engine. Their inputs (session status, "is this PR open/failing/merged", open issues, review comments) are already available today from `pr_status.py`/`pr_poller.py` + `gh`.

So: **ship Triage + Dispatch + PR-broke-fix on the current PR data with Plotroom left running (frozen) as a separate tab; then do the strangler port** to fold Plotroom in and unlock the richer stall/ship signals.

**Why this matters for a 5–10 hr/week solo dev:** the committed order front-loads the riskiest work (ETA-parity porting) and the lowest-value work (cost explorer) before any felt win — the textbook abandonment trap. The honest failure mode of Option B is stalling at *"I successfully rebuilt Plotroom in Python"* having shipped zero automations.

**If Option B (committed order) is kept**, set a hard tripwire: *if no automation has shipped by week 8, stop porting and pivot to automations.*

---

## 2. Prioritization — MoSCoW

### Combined-app capabilities
| Capability | MoSCoW | Justification |
|---|---|---|
| Triage Lens (US-1) | **Must** | Pain #1; cheapest payoff-per-hour |
| PR state on card via live branch (US-2) | **Must** | The primitive all three automations reuse; ~80% built |
| Work Dispatcher chips (US-5/6) | **Must** | Pain #1 (tied); extends the existing suggestion surface |
| PR-broke → fix chip (US-9) | **Must** | Core slice of the most-wanted automation |
| Single poller / decommission duplicate (US-17) | **Must** | Constitution VI; halves maintenance |
| Push for top-tier only (US-3/10) | **Should** | High away-from-desk value; can lag the in-app version |
| Unified Pipeline view + ETAs (US-13/14) | **Should** | Plotroom already provides this; low marginal pain until Plotroom retired |
| Dispatcher ranking + collision-avoidance (US-7/8) | **Should** | QoL on a working dispatcher |
| prod-live / overdue / regression (US-11/12) | **Could** | Pleasant, not painful |
| Queue-train strip, `behind N` (US-15) | **Could** | Cosmetic richness |
| Snooze/dismiss triage (US-4) | **Could** | Matters only at high triage volume |
| Cost explorer / kiosk / App mode / webhooks / digest | **Won't (v1)** | ~Half of Plotroom's surface; zero overlap with the automations; pure carrying cost for one user |

### Plotroom features — port now / defer / drop
| Feature | Verdict | Reasoning |
|---|---|---|
| Pipeline stage machine | Port (deferred) | Needed only when retiring Plotroom |
| ETA engine + learned history | Port (deferred) | Crown jewels; hardest port; highest drift risk; don't touch until committed to retiring Plotroom |
| SSE live stream | **Drop / reuse** | ccswitch already has a live WebSocket channel; reuse rather than re-port frame-dedup |
| Notification taxonomy | Port (thin slice now) | Only `ci-failed`, `queue-blocked/stalled`, `prod-live` needed for automations; defer the rest |
| Daily digest | **Drop** | Not selected in brainstorming |
| CI cost explorer + actuals + pool-rate relay | **Drop** | Biggest hours-sink, zero automation overlap; clearest cut |
| Kiosk mode | **Drop** | Out of scope; vanity for a one-person fleet |
| GitHub App mode | **Drop** | `gh` token already works; enterprise-shaped complexity |
| Webhook receiver | **Drop** | Polling adequate; needs tunnel+HMAC for marginal latency |
| In-repo `.pr-dashboard.yml` | Defer/maybe drop | Useful only for repos you don't control; you control all yours |
| Settings panel hot-config | **Drop/minimize** | ccswitch has its own config; don't port a parallel one |
| Deploy ancestry | Port (deferred, conditional) | Only for QA/prod stages; port when wiring prod-live |

**Net: roughly half of Plotroom's surface should never be ported.**

---

## 3. MVP

**The smallest combined app that delivers daily value = Triage + Dispatch + PR-broke-fix, riding on the PR state ccswitch already computes today, with Plotroom left running untouched as a separate tab.**
- US-2 PR state on card — extend the existing `pr_poller.py`/`pr_status.py` (already does this).
- US-1 ranked needs-me — pure read-layer fusion of session status + that PR state.
- US-5/6 dispatch chips — extend the existing `latest_suggestions` + ghost-text + Resume/chevron surface.
- US-9 PR-broke → `gh-fix` chip — needs only "did this session's live-branch PR's CI fail?" (already available).
- US-3 push top-tier — reuse ccswitch's push path, or smallest possible.

---

## 4. Part-time roadmap (~5–10 hrs/week; effort = review/decision hours, the real bottleneck)

| Milestone | Maps to | Review effort | Rationale |
|---|---|---|---|
| **M1 — Live PR state on every session card** | US-2, US-17 (partial); Phase 0 data foundation (Part A only) | **~1–2 wks** | Phase 0 Part A (extend `PullRequestInfo`) is cheap (~6 tidy TDD tasks). Do this part. **Skip the parity harness for now** — it earns its keep only when porting the tuned engine (deferred). |
| **M2 — 🎯 FIRST useful milestone: Triage Lens (read-only)** | US-1, US-3 | **~1–2 wks** | Pure fusion of data already in hand. Ship and live on it for a couple weeks before building more. |
| **M3 — Work Dispatcher chips** | US-5, US-6 | **~2–3 wks** | Extends existing suggestion surface; wires `prioritize-issues`/`gh-fix`/`pr-review`. Most decision-heavy to tune ("which task is next?") — babysit it. |
| **M4 — PR-broke → fix chip + stall/ship push** | US-9, US-10, US-11 (thin notification slice) | **~2 wks** | Most-wanted automation on thin integration. Port only `ci-failed`/`queue-blocked`/`prod-live` detection. |
| **M5 — Strangler port: pipeline engine + Pipeline view** | Phases 0(harness)–3; US-13/14/15 | **~6–10 wks, spread** | NOW build the parity harness and port `pipeline-model/` + `estimator/` + frontend, agent-driven with the harness as the gate. Retires Plotroom, enriches views — after automations already pay rent. |
| **M6 — Decommission Plotroom; confirm drops** | Phase 4 (minus cost explorer) | **~1 wk** | Turn off the second app. Do NOT port cost/kiosk/App/webhooks/digest. |

**Time to first value (M2): ~3–4 calendar weeks part-time. To all three automations (M4): ~8–10 weeks.** The heavy port (M5) is deferred and "optional-feeling" by the time it's reached — the intended safety.

**Cross-cutting:** M1 must keep `PullRequestInfo` RPC-shape-stable (it crosses the daemon↔edge boundary). M3/M4 must extend `latest_suggestions` in place, not add a parallel suggestion store. M5's estimator cutover must migrate `history.db` (history continuity is a release gate — RR-3).

---

## 5. Success metrics (baseline before M2)

- **Time-to-notice a broken PR:** from "next time I look" (hours) → **< 5 min** (push) / **< 1 check-in** (in-app). *Headline metric.*
- **Context-switches to GitHub/Plotroom per day:** → near 0 after M1.
- **Idle-session minutes** (sessions sitting idle/done with no next task): meaningful reduction (~50%) after M3.
- **Manual dispatch actions per day** (copy-paste issue+skill) → ≥80% replaced by one-tap chip approvals.
- **PRs stalled >24h unnoticed per week:** → 0 after M4.
- **Active ccswitch minutes per PR merged:** should fall (ship more per scarce hour).
- **Anti-vanity guard:** # of the three automations actually shipped and in daily use. If 0 after 8 weeks because of porting, the roadmap has failed regardless of code quality.

---

## 6. Delivery / scope risks (not technical — see architecture doc for technical risks)

| Risk | L | Mitigation |
|---|---|---|
| **The rewrite trap** — months sunk into the Level-3 port before any automation ships | **High** (central risk) | Re-sequence: automations first (M2–M4), heavy port last (M5). If Option B kept, hard tripwire at week 8. |
| **Scope creep porting Plotroom's full surface** | **High** | Explicit DROP list (§2). Write "WON'T PORT" into the spec so future-you doesn't re-add them. |
| **Maintaining two apps mid-migration** (two pollers, two budgets, drift) | **Medium** | Keep Plotroom running-but-frozen as a read-only oracle during M1–M4; single-poller obligation (US-17) satisfied only at M6 — accept temporary duplication consciously, with an end date. |
| **Motivation / abandonment** (part-time, solo) | **High** | M2 engineered to land in ~3–4 weeks to bank an early win; each milestone independently shippable and useful (US-18). |
| **Behavioral drift in the deferred port poisoning a working app** | **Medium** | Build the parity harness at M5 when it gates something; keep frozen-Plotroom as the live oracle until M5 passes. |
| **Review-bandwidth bottleneck** (agents out-produce review; dispatcher floods low-quality launches) | **Medium** | Dispatcher proposes, never auto-launches (US-19); start top-1 only; expand ranking (US-7) once trusted; approval is the rate-limiter by design. |

---

## 7. Bottom line

The engineering plan is sound; the **sequencing** is the threat. The three automations are the product, ride on primitives that already exist, and do **not** require the Level-3 rewrite to finish first. Ship them on a thin integration, get daily value in ~a month, and let the (real, worthwhile) Level-3 port happen afterward as consolidation — not as a gate the payoff is trapped behind. Cut ~half of Plotroom's surface outright.

**Decision needed (requirements §0):** confirm **Option A (value-first, recommended)** or **Option B (committed foundation-first order)** before this roadmap is treated as committed.
