---
slug: sdlc-governance-evals
title: SDLC Engine, Governance & Evals
state: delivering
links:
  code:
    - server/sdlc/engine.py
    - server/sdlc/governance.py
    - server/sdlc/review_policy.py
    - server/sdlc/prompts.py
    - server/sdlc/evals.py
    - server/sdlc/control_bands.py
    - server/sdlc/bands.py
    - server/sdlc/bands.yaml
    - server/sdlc/triage_store.py
    - server/api/sdlc.py
    - server/automations/sdlc_intake.py
    - server/automations/sdlc_synthesizer.py
    - server/automations/control_band_monitor.py
    - web/src/initiatives/IncidentQueue.tsx
    - .claude/REVIEW.md
    - evals/README.md
    - web/src/automations/AutomationsTab.tsx
  prs: [808, 810, 812, 813, 814, 819, 824, 818, 809]
---

# SDLC Engine, Governance & Evals

**Problem** — An autonomous development loop that can select work, write code, and land PRs
is only safe if something keeps it honest: it must move an initiative through real
lifecycle stages (intent → spec → plan → build → deploy → maintain) with a human gate at
each one, it must be *forbidden* from doing dangerous things (force-push, editing the
constitution or managed settings, self-approving a prod release), its code review must
follow a stated policy rather than an LLM's mood, and there must be a measurable pass-rate
gate so a regression in the agent's own behavior is caught before it ships. Without this,
the loop can silently drift, rubber-stamp its own work, or reintroduce a past incident.

**Target users** — The operator/owner who wants the self-driving SDLC to advance
initiatives with an approval tap at each stage, and the autonomous agents themselves, whose
tool calls, review comments, and completion claims are constrained by these gates.

**Desired outcome** — A pure stage engine decides the single next action per initiative
(propose an artifact PR, await approval, hand off to build, or done) and never auto-advances
past a human gate in v1. Per-stage seed prompts turn each upstream artifact into the next,
writing only to `docs/sdlc/<slug>/<kind>.md` on a fresh branch as one non-draft PR.
Governance maps environments to permission tiers (dev full-auto / staging acceptEdits / prod
interactive), enforces separation-of-duties on prod releases, and pins non-negotiable deny
rules into managed settings. A version-controlled REVIEW.md drives PR review (allowed
categories, severity tags, a nit cap, no self-approval, and CLAUDE.md seeding when a pattern
recurs). An eval suite with a pass-rate threshold gates the loop, and every production
incident becomes a regression eval.

**Success metrics** — (proposed, no baseline recorded)
- Initiatives advanced only via an explicit human gate tap (no unauthorized auto-advance).
- Prod releases blocked unless a distinct approver is recorded (separation of duties holds).
- Governance deny-rule coverage: force-push / constitution / managed-settings edits always
  blocked — and false-positive rate on prose driven down (open issue #832).
- Eval gate pass-rate stays at/above threshold; regressions added per incident (coverage
  grows monotonically, no incident without a regression case).
- Review findings conform to REVIEW.md (in-policy categories, nit cap ≤ 3, no self-approval).

**Non-goals** — v1 is deliberately propose-only and human-gated: the SDLC automations do not
auto-approve or auto-advance. The Maintain loop detects and files autonomously, but its
strongest response is still to *queue* an incident so Intake opens an `intent.md` PR —
which is itself a proposal behind the same human gate. Its own automation ships off, and
`monitor` mode exists so an operator can watch the queue fill before letting any tier act.
The engine and
governance/review/eval cores are pure (no I/O) — they decide, they do not act; spawning,
recording, and GitHub mutations live in the automations and API. Evals are not a substitute
for CI tests; static eval checks run in CI, agent evals run out-of-band.

**Notes** —
- **State is `delivering`, not `live`:** the intake → synthesizer → governance → evals →
  review-policy chain is landed (Phase 2 design #808; stage engine + prompts #810; intake
  #812/#813; API + build-handoff correlation #814; governance & evals control plane #819
  closing Phase 3 epic #805; REVIEW.md policy + pr_doctor `@claude` wiring #824; web
  Initiative board #818), and the **Maintain loop (#804) now closes the cycle**. What still
  holds the state back is **#832**, below: the governance gate meant to keep the loop honest
  has a live correctness bug, and a loop that can now re-enter itself autonomously should not
  be called `live` while the gate around it misfires.
- **Open governance gap #832:** the PreToolUse governance gate false-positives on prose — it
  blocks the agent from filing issues and even from reading its own source. This is a live
  correctness bug in the very gate meant to keep the loop honest; it must be tightened before
  governance can be trusted at higher autonomy.
- The engine (`engine.py`) mirrors `pr_doctor_engine`'s pure-decision shape: a frozen
  `StageAction` per initiative, `AwaitApproval` forever until a human advance. Prompts
  (`prompts.py`) inject org skills as binding constraints at the design/spec stage.
- Governance (`governance.py`) has three surfaces: permission tiers per environment, an
  `evaluate_release()` approval gate (approver ≠ author, constitution IV), and managed
  non-negotiable deny rules that must exist in `.claude/managed-settings.json`. Review policy
  (`review_policy.py`) parses `.claude/REVIEW.md` into allowed categories (bug/security/
  compliance), severity tags with block/no-block rules, a default nit cap of 3, Initiative-PR
  detection, and CLAUDE.md seeding when a major-severity pattern recurs across ≥2 PRs.
- Evals (`server/sdlc/evals.py`) aggregate `EvalCase`/`EvalResult` into an `EvalReport` whose
  gate passes only when `pass_rate >= threshold AND total > 0`. The corpus is one YAML per
  case under `evals/cases/` (static shell checks) and `evals/regressions/` (per-incident);
  `scripts/run_evals.py` runs them via `.github/workflows/evals.yml` on PRs touching
  agent-config paths. NOTE: the plan text references a `server/sdlc/`-internal harness; the
  runnable harness on disk is `scripts/run_evals.py` over the `evals/` corpus.
- The Initiative board / list artifact-chain UI (#818) is served by `/api/sdlc`
  (`server/api/sdlc.py`) and rendered under the Automations surface
  (`web/src/automations/AutomationsTab.tsx`); the human gate tap is `POST
  /initiatives/{slug}/advance`.

**Evidence** —
- Engine / prompts / governance / review / evals:
  `server/sdlc/engine.py`, `server/sdlc/prompts.py`, `server/sdlc/governance.py`,
  `server/sdlc/review_policy.py`, `server/sdlc/evals.py`.
- Automations / API / policy doc / eval corpus:
  `server/automations/sdlc_intake.py`, `server/automations/sdlc_synthesizer.py`,
  `server/api/sdlc.py`, `.claude/REVIEW.md`, `evals/README.md`.
- PRs: #808 (AI-native SDLC design), #810 (pure stage engine + seed prompts, #797/#798),
  #812 (intake → proposed intent.md, #799), #813 (intake + synthesizer wired in), #814
  (/api/sdlc + build-handoff correlation), #819 (Phase 3 governance & evals control plane,
  closes #805), #824 (REVIEW.md policy + pr_doctor @claude-comment wiring), #818 (web
  Initiative board), #809 (Initiative + InitiativeArtifact spine, #796).
- Maintain loop (#804): `server/sdlc/control_bands.py` (Western Electric detection),
  `server/sdlc/bands.py` + `server/sdlc/bands.yaml` (the version-controlled tier policy),
  `server/sdlc/triage_store.py`, `server/automations/control_band_monitor.py`,
  `GET /api/sdlc/triage` + `POST /api/sdlc/triage/{id}/action`,
  `web/src/initiatives/IncidentQueue.tsx`.
- Open issues: #832 (governance PreToolUse false-positives on prose — now the gate on `live`).
