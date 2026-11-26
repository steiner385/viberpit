// web/scripts/e2eFlake.mjs — the rules of the E2E flake budget (#1412, G14).
//
// Pure functions; `check-e2e-flake.mjs` is the fs + exit-code shell around them.
//
// Why the JSON reporter and not the junit one CI already uploads: junit folds a test
// that failed and then passed on retry into a plain pass, so it cannot tell a flake
// from a clean run. Playwright's JSON report says `flaky` outright.
//
// Definition. A *run* of a test is one entry in a report, once per project. It is:
//   - `expected`   passed on the first attempt            → clean
//   - `flaky`      failed, then passed on a retry         → FLAKE
//   - `unexpected` never passed                           → failure (a defect, not noise —
//                  reported separately, never folded into the flake rate)
//   - `skipped`    not executed                           → excluded from the denominator
// flake rate = flaky / (expected + flaky + unexpected).

/** Proposed in #1412: a subset may be promoted to a required check below this. */
export const FLAKE_BUDGET = 0.01

/**
 * A rate over a handful of runs is anecdote: 0 flakes in 20 runs is consistent with a 10%
 * flake rate. The rule of three says that observing 0 events in n trials only bounds the
 * true rate below ~3/n at 95% confidence, so proving < 1% needs n ≥ 300.
 */
export const MIN_RUNS_FOR_VERDICT = 300

const STATUSES = new Set(['expected', 'flaky', 'unexpected', 'skipped'])

/** Flatten a Playwright JSON report into `{ project, title, status }` entries. */
export function parseReport(report) {
  const out = []
  const walk = (suite, titles) => {
    const here = suite.title ? [...titles, suite.title] : titles
    for (const spec of suite.specs ?? []) {
      for (const t of spec.tests ?? []) {
        // `status` is Playwright's own roll-up of the retries; an unknown value would be a
        // reporter change we must not silently count as clean.
        if (!STATUSES.has(t.status)) throw new Error(`unrecognised test status "${t.status}" in "${spec.title}"`)
        out.push({ project: t.projectName ?? 'unknown', title: [...here, spec.title].join(' › '), status: t.status })
      }
    }
    for (const child of suite.suites ?? []) walk(child, here)
  }
  for (const suite of report.suites ?? []) walk(suite, [])
  return out
}

/** Count entries per project and overall. */
export function tally(entries) {
  const blank = () => ({ runs: 0, flaky: 0, failed: 0, skipped: 0 })
  const byProject = {}
  const total = blank()
  for (const e of entries) {
    const bucket = (byProject[e.project] ??= blank())
    for (const b of [bucket, total]) {
      if (e.status === 'skipped') { b.skipped += 1; continue }
      b.runs += 1
      if (e.status === 'flaky') b.flaky += 1
      if (e.status === 'unexpected') b.failed += 1
    }
  }
  return { byProject, total }
}

/** flaky / runs, or `null` when nothing ran (0/0 is not a rate). */
export function flakeRate({ runs, flaky }) {
  return runs === 0 ? null : flaky / runs
}

/**
 * The verdict for one bucket.
 *   - `failing`    something never passed — fix the defect before measuring noise
 *   - `insufficient` too few runs to claim a rate either way
 *   - `over`       measured flake rate exceeds the budget
 *   - `within`     enough runs and under budget → eligible for promotion
 */
export function verdict(bucket, { budget = FLAKE_BUDGET, minRuns = MIN_RUNS_FOR_VERDICT } = {}) {
  if (bucket.failed > 0) return 'failing'
  const rate = flakeRate(bucket)
  if (rate === null || bucket.runs < minRuns) return 'insufficient'
  return rate > budget ? 'over' : 'within'
}

export function formatReport({ byProject, total }, opts = {}) {
  const pct = (b) => {
    const r = flakeRate(b)
    return r === null ? '   n/a' : `${(r * 100).toFixed(2)}%`.padStart(6)
  }
  const row = (name, b) =>
    `${name.padEnd(10)} runs ${String(b.runs).padStart(5)}  flaky ${String(b.flaky).padStart(3)}  failed ${String(b.failed).padStart(3)}  rate ${pct(b)}  → ${verdict(b, opts)}`
  return [
    ...Object.keys(byProject).sort().map((p) => row(p, byProject[p])),
    row('ALL', total),
  ].join('\n')
}
