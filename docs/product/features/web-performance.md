---
slug: web-performance
parent: observability
title: Web Performance — Field Vitals and Bundle Budget
state: live
links:
  code:
    - web/src/obs/vitals.ts
    - web/src/obs/routeTemplate.ts
    - web/src/obs/WebVitalsCollector.tsx
    - web/src/features/sessions/transcript/markdownChunk.ts
    - web/scripts/bundleBudget.mjs
    - web/scripts/check-bundle-budget.mjs
    - web/bundle-budget.json
    - server/obs/web_vitals.py
    - server/api/web_vitals.py
  issues: [1411]
---

# Web Performance — Field Vitals and Bundle Budget

Know what the browser actually experienced, and fail the build when the critical path grows.

**Problem** — the 2026-09 UI audit (#1411) could say the main JS bundle was 453kB gzip and that every
route imported eagerly, and could say nothing whatever about whether that hurt anyone. No field
metrics were collected, so LCP, INP and CLS were all "unknown" in the report; and nothing gated the
bundle, so weight accumulated invisibly — by the time this landed the entry chunk had drifted to
547kB gzip with no PR ever having been told it was the one that did it. The two gaps compound: with
no field data, every splitting decision is a guess, and with no budget, the guesses never have to be
right.

**Target users** — whoever is holding the question "is the app slow, and for whom?": the operator on
a phone over the tailnet, and the next contributor about to add a dependency to a shared component.

**Desired outcome** — real loads report LCP/INP/CLS/TTFB/FCP with enough context to segment by route
shape, device tier and browser, and carrying nothing that identifies a user or a session; a p75 is
readable per segment against the published thresholds; and CI fails a PR that pushes the pre-paint
payload past a committed ceiling that only ever moves down.

**Success metrics** — p75 field LCP ≤ 2.5s, INP ≤ 200ms, CLS ≤ 0.1 (#1411), read per
route/device/browser rather than pooled. Lab, measured against `origin/master` at the time this
landed: entry chunk 547.2 → 142.5kB gzip (target ≤ 250, met), full pre-paint payload 623.2 →
255.9kB gzip (6kB above the same target, ratcheting), initial CSS 24.2 → 14.2kB gzip.

**Non-goals** — a time-series backend or dashboards (this is a p75 endpoint over a bounded table,
not Prometheus); RUM for anything other than Core Web Vitals; per-user performance profiles —
samples are deliberately un-attributable; replacing the advisory Lighthouse CI job, which measures a
synthetic load and answers a different question.

**Notes** —

- **What a sample carries** (`web/src/obs/vitals.ts`): metric, value, route SHAPE, device tier, plus
  a batch envelope with nav type, build sha, a random per-page-load id and the count/bytes of JS
  fetched after `load`. There is no URL, no query string, no identity, no session id and no
  User-Agent. The browser family is derived from the request header at ingest and the header
  discarded. #1411 requires that prompts, tokens and account names are never collected, and the
  table shape is where the promise is kept: a column that does not exist cannot be filled in later.

- **Route shapes come from the router, not a pattern list** (`web/src/obs/routeTemplate.ts`): the
  collector is handed react-router's own `useParams()` and replaces each param value in the path
  with its name, so `/sessions/<uuid>` becomes `/sessions/:id`. A second table of path patterns
  would be a copy of `shell/routes.tsx` that goes stale silently the day a route is added — the new
  route's samples would land under a catch-all and nobody would notice. A digit-bearing segment that
  was not a declared param is scrubbed anyway, in the browser and again on the server.

- **The kill switch stops the measurement, not just the ingest**
  (`VIBERPIT_WEB_VITALS_ENABLED`): the flag is published on `GET /api/config`, and the collector
  reads it before installing any PerformanceObserver. The ingest endpoint checks it too, because a
  stale bundle in an open tab keeps reporting for as long as that tab lives.

- **Thresholds live on the server** (`server/obs/web_vitals.py`): `web-vitals` hands the browser a
  good/needs-improvement/poor rating for free, and it is deliberately dropped. Stored rows have to
  be classifiable later — when the window moves, when a threshold is retuned — so the server needs
  the table regardless, and two copies of a threshold is one too many. p75 is nearest-rank, matching
  CrUX, so a number here is comparable with one off a Chrome dashboard. Under ten samples a segment
  reports its p75 with `meetsTarget: null` rather than a verdict: "not enough data" is not "fails".

- **Retention is opportunistic** (`server/api/web_vitals.py`): rows older than
  `VIBERPIT_WEB_VITALS_RETENTION_DAYS` (30) are swept on the ingest path, at most once an hour per
  process. A dedicated background loop would be another thing to supervise for a DELETE that takes
  milliseconds, and the table only grows when ingest runs.

- **The budget gates the initial payload, not the entry chunk**
  (`web/scripts/bundleBudget.mjs`): rolldown splits shared code out of the entry and has
  `index.html` `modulepreload` it, so moving a page behind `lazy()` can shrink `index-*.js` by 70%
  while the browser downloads exactly the same bytes before first paint. `initialJs` — entry plus
  every preload — is the honest number and the one that gates. The entry is still reported because
  it is what the build log shows and what #1411 quoted.

- **Ceilings only go down** (`web/bundle-budget.json`): the same rule as `scripts/brand_ratchet.py`,
  for the same reason — a budget that can be raised to make a build pass is a comment, and the raise
  always looks justified in the moment. `npm run budget -- --update` banks a shrink; raising a
  ceiling is a deliberate, reviewable edit. Ceilings sit ~5% above the measured size so ordinary
  growth does not teach everyone to bump the number reflexively.

- **What was split, and what deliberately was not**: the transcript markdown renderer
  (`transcript/markdownChunk.ts` — react-markdown, the remark/rehype chain, highlight.js, KaTeX;
  ~240kB gzip), noVNC (`vnc/vncClient.ts`, loaded on `connect()`), the VNC viewer component
  (`browser/DeferredBrowserTab.tsx`), and the Roadmap / Delivery / Pipeline / Settings routes
  (`shell/routes.tsx`). The Sessions pages and the nav-state modules stay eager: `/sessions` is
  where the app opens, and deferring it would trade a smaller entry chunk for a second round trip on
  the critical path — a worse LCP measured as a better bundle, which is exactly the trade the field
  collector exists to stop anyone making blind. The markdown chunk is warmed by
  `prefetchMarkdown()` when a transcript mounts, so its plain-text Suspense fallback is rarely seen.

**Evidence** —
- `web/src/obs/vitals.ts`, `web/src/obs/routeTemplate.ts`, `web/src/obs/WebVitalsCollector.tsx` —
  collection, redaction and the config-gated mount.
- `server/obs/web_vitals.py` — thresholds, nearest-rank p75, segmentation, UA family.
- `server/api/web_vitals.py` — `POST /api/web-vitals`, `GET /api/web-vitals/summary`, server-side
  re-scrubbing, retention sweep.
- `server/db/models.py:WebVitalSample` + `server/db/migrations/versions/0064_web_vital_samples.py`.
- `web/scripts/check-bundle-budget.mjs` + `web/bundle-budget.json` — the gate, run by CI's
  `frontend` job after the build.
- Issue #1411 (UI audit G13).
