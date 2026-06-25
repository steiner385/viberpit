# Combined-App (ccswitch ⨉ Plotroom) — Where It Actually Stands

> **Living status snapshot.** The dated docs under `specs/` (roadmap, requirements,
> architecture, user-stories) describe the *intended* program; the dated `plans/` describe
> *how* each slice is built. **This page is the one place that says what is actually shipped,
> what is deferred, and the decision in front of you — read this first.** Last updated:
> 2026-06-24.

## TL;DR

- We did **not** merge the two codebases. By deliberate choice ("value-first" / Option A) we
  shipped the high-value **automations on ccswitch's own data first** and **deferred** the
  risky port of Plotroom's engine.
- **ccswitch and Plotroom are still two separate running apps.** Plotroom is **not** sunset.
- The only piece of Plotroom's tuned logic ported so far is the **stage classifier**
  (`ci` / `ready` / `parked` / `merged`), which now runs natively in ccswitch and shows on the
  PR pill.
- **You lose nothing by stopping here.** The painful gap — session ↔ the PR it shipped — is
  now closed *inside* ccswitch. The "one app, turn Plotroom off" end-state is real but optional,
  multi-week, and currently **blocked on a richer test surface**, not on effort.

## Why the order looks backwards

A textbook strangler-fig migration ports the engine first, then builds features on it. We
**inverted** that on purpose: ship the loop-closing automations on data ccswitch already had,
so the payoff arrived without waiting on (or risking) the engine rewrite. The cost is exactly
the confusion this page exists to clear up: "are we consolidated yet?" has a genuinely split
answer — *automations yes, engine no.*

## Where each capability lives right now

| Capability | In ccswitch (live) | Only in Plotroom (still) |
|---|---|---|
| **Triage** — rank sessions by "what needs me" | ✅ M2a (#233) | — |
| **Push to phone** — top-tier needs-you + PR-broke alerts | ✅ M2b / M2b.2 (#236 / #243) — built; **dormant until `CCSWITCH_PUSH_WEBHOOK_URL` is set** | — |
| **Work dispatch** — propose next issue / broken-PR / review to idle sessions | ✅ M3a/b/c (#247 / #248 / #250), collision-avoidance (#252) | — |
| **PR stage label** (ci / ready / parked / merged) on session cards | ✅ M5 stage machine (#258 / #269) | (also computed here) |
| **ETA** ("merges in ~8 min") | ❌ | ✅ estimator + learned `history.db` |
| **Merge-queue position** ("behind 3") | ❌ | ✅ |
| **Deploy / ship tracking** (QA → prod, ancestry) | ❌ | ✅ |
| **Pipeline / metro-map visual UI** | ❌ | ✅ Plotroom's React frontend |

**So today:** ccswitch ranks what needs you, can ping your phone, suggests the next task for an
idle session, and tags each PR's coarse stage. For ETAs, queue depth, deploy/ship status, and
the visual pipeline, you still open Plotroom (port 4400).

## What's shipped (durable on `origin/master`)

All merged — none of this lives only in a working tree:

- **M1** live-branch PR keying (#231) · **M2a** Triage Lens (#233) · **M2b** push sink (#236) ·
  **M2b.2** PR-broke push (#243)
- **M3a/b/c** Work Dispatcher across issue / broken-PR / review sources (#247 / #248 / #250),
  **US-8** collision-avoidance (#252)
- **M5 Phase 0** parity harness + PR-snapshot foundation (#258); **M5 stage machine wired**
  end-to-end into the live poller + PrPill (#269)
- Supporting fixes: #206 shutdown leaks (#253 / #255 / #257), triage/closed-PR (#259), and the
  deploy/snapshot work (#262 / #265 / #268)

## What's deferred — and *why* (this is the crux)

The remaining engine slices (ETA, merge-queue, deploy/ancestry, the Pipeline UI) are **blocked,
not abandoned** — and the blocker is **verification, not effort**:

> Every tuned heuristic is ported under a **parity gate**: the Python output must match the live
> Plotroom oracle (`GET /api/state`) before cutover. Your single watched repo
> (`steiner385/cc-account-switcher`) is too quiet an oracle to verify against — **no merge
> queue**, **no deploy environments**, and **rarely a mid-CI PR with enough timing history**.
> You can't safely cut over a heuristic you can't measure.

The stage machine cleared this bar (229/229 PRs matched on open+merged); the rest can't, yet.
Unblocking is **operational**: point the live Plotroom at a busier repo (or wait for a busier
CI period), then the deferred slices become executable. Plan: `plans/2026-06-24-m5-phase1-engine-port.md`.

## Does this sunset Plotroom?

**No — and not soon.** Decommissioning Plotroom is the **last** phase (Phase 4 in the roadmap)
and only happens after the full engine port **and** the Pipeline-UI merge. Until then Plotroom
is doing real work two ways: it's the **parity oracle** the port is measured against, *and* it's
still the **only** place ETA / queue / deploy / pipeline-view exist.

## Decisions in front of you

1. **Turn on phone alerts** — set `CCSWITCH_PUSH_WEBHOOK_URL` on the ccswitch service. The push
   code is built and tested; this one env var activates it. (Operational, yours — it's your
   webhook endpoint.)
2. **Decide whether to pursue the full engine port.** If yes, the unblock is operational (busier
   oracle repo) and the slice plan is written. If no, you keep two apps with the session ↔ PR gap
   already closed — a perfectly stable resting point.

## Pointers

- Program rationale & phases: `specs/2026-06-23-ccswitch-plotroom-merge-overview-design.md`,
  `specs/2026-06-23-combined-app-roadmap.md`
- Value-first sequencing decision: `specs/2026-06-23-combined-app-requirements.md` §0,
  `plans/2026-06-24-combined-app-m1-m4-value-first.md`
- Engine-port slice plan + **must-port invariants**: `plans/2026-06-24-m5-phase1-engine-port.md`
- Why the parity model was reworked: `specs/2026-06-23-plotroom-merge-phase0-parity-foundation-design.md`
  (superseded note) + `evidence/2026-06-24-parity-phase0-finding.json`
- Parity harness code: `server/github/parity/` · stage classifiers: `server/pipeline/stage.py`
