---
slug: session-insights
title: Session & CI Insights
state: live
links:
  code:
    - web/src/components/InsightsView.tsx
    - web/src/components/CheckTable.tsx
    - server/api/pipeline.py
    - server/automations/cicd/stats.py
    - server/automations/consolidation_engine.py
    - server/api/automations.py
    - web/src/components/ConsolidatePanel.tsx
    - web/src/components/session/InsightCallout.tsx
  prs: [386, 389, 394, 470, 555, 38, 77, 89, 568]
---

# Session & CI Insights

**Problem** — Across many sessions and repos, the raw firehose of turns, transcripts, PRs,
and CI runs is too much to read. The operator needs the noise consolidated into digestible
signals: which checks are slow or flaky, where durations are regressing, which sessions
surfaced a genuine insight, and which open PRs can be safely combined.

**Target users** — The operator triaging fleet-wide health and deciding where to spend
attention — reading trends and callouts rather than individual transcripts.

**Desired outcome** — An Insights view that summarizes CI check health per repo over a
selectable window (24h/7d/14d/30d): slowest checks (p50/p90, variability), flakiest checks,
duration regressions, and runner-wait health. Alongside it, transcript-level "★ Insight"
callouts lift genuine findings out of individual sessions, and a consolidation engine
clusters open PRs into low-risk groups the operator can act on in one click.

**Success metrics** — (unknown — needs owner input). The view itself is the metric surface:
p50/p90 latencies, flake percentage (flaky/total commits), duration-regression ratios, and
runner-wait times are what it exposes for the operator to judge.

**Non-goals** — Per-token usage billing analytics (usage-aware account rotation lives
elsewhere). A generic BI/query tool — Insights is a fixed set of curated check-health slices
plus transcript callouts, not ad-hoc analytics.

**Notes** — Delivered in slices: Check Health tab with slowest+flakiest checks (slice A,
#386), then duration regressions + runner-wait health (slice B, #389); the tabs were added
to the wide-screen `DesktopShell` (#394). The check-insights data is computed server-side
(`orch.check_insights(repo, window)` in `server/api/pipeline.py`, stats in
`server/automations/cicd/stats.py`) and rendered by `InsightsView` via the `useCheckInsights`
hook. Transcript "★ Insight" callouts are a separate, older signal: styled callouts in the
transcript (#38) with delimiter-detection refinements for backtick/em-dash/inline-title
openers (#77, #89). PR consolidation is a pure engine (`consolidation_engine.py`) surfaced
in the PR Doctor card as cluster chips + multi-select (`ConsolidatePanel`, #470), flagging
PRs already being consolidated and linking the session (#555).

**Evidence** —
- Insights view: `web/src/components/InsightsView.tsx`, `web/src/components/CheckTable.tsx`,
  hook `useCheckInsights` in `web/src/api/hooks.ts`.
- Server: `server/api/pipeline.py` (`check_insights` endpoint),
  `server/automations/cicd/stats.py`.
- Consolidation: `server/automations/consolidation_engine.py` (pure clustering),
  `server/api/automations.py` (`/pr-doctor/consolidation`, `/pr-doctor/consolidate`),
  `web/src/components/ConsolidatePanel.tsx`.
- Transcript callouts: `web/src/components/session/InsightCallout.tsx`.
- PRs: #386/#389 (Insights slices A/B), #394 (Insights tab in DesktopShell), #470/#555
  (ConsolidatePanel / flag-already-consolidating), #38/#77/#89 (★ Insight transcript
  callouts + delimiter detection), #568 (consolidate RPC edge fix).
