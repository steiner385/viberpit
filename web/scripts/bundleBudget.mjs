/**
 * Bundle budget — the pure half (#1411).
 *
 * Parsing and arithmetic only, so `bundleBudget.test.mjs` can pin the rules
 * without a build in the way. `check-bundle-budget.mjs` is the thin shell that
 * reads `dist/` and gzips.
 *
 * ## Why the entry chunk is not the measurement
 *
 * The obvious budget is "how big is `index-*.js`", and it is the wrong number.
 * Rolldown splits shared code out of the entry and has `index.html`
 * `modulepreload` it, so moving a page behind `lazy()` can shrink `index-*.js`
 * by 70% while the browser still downloads exactly the same bytes before first
 * paint. A budget on the entry alone would have called that a win.
 *
 * What the browser actually fetches before it can render is everything
 * `index.html` references: the entry script, every `modulepreload`, and every
 * stylesheet. That set — `initialJs` and `initialCss` — is the budget. The
 * entry is still reported, because it is what the build log shows and a reader
 * comparing the two needs to see both, but it is not gated.
 */

/** The assets `index.html` makes the browser fetch before it can paint. */
export function parseInitialAssets(html) {
  const scripts = [...html.matchAll(/<script\b[^>]*\bsrc="([^"]+)"/g)].map((m) => m[1])
  const links = [...html.matchAll(/<link\b[^>]*>/g)]
  const modulepreloads = []
  const stylesheets = []
  for (const [tag] of links) {
    const href = /\bhref="([^"]+)"/.exec(tag)?.[1]
    if (!href) continue
    const rel = /\brel="([^"]+)"/.exec(tag)?.[1] ?? ''
    const as = /\bas="([^"]+)"/.exec(tag)?.[1] ?? ''
    // `prefetch` is deliberately excluded: it is idle-time, after-paint work by
    // definition, so charging it to the initial payload would price a
    // deliberate optimisation as a regression. A plain `preload` only counts
    // when it is a SCRIPT — a preloaded font is real pre-paint weight but it is
    // not JS, and filing it under `initialJs` would make the number mean two
    // things at once.
    if (rel === 'modulepreload' || (rel === 'preload' && as === 'script')) modulepreloads.push(href)
    else if (rel === 'stylesheet') stylesheets.push(href)
  }
  return { scripts, modulepreloads, stylesheets }
}

/** kB, one decimal — the unit every budget number in this repo is written in. */
export function toKb(bytes) {
  return Math.round((bytes / 1024) * 10) / 10
}

/**
 * Compare measurements against the budget.
 *
 * `measured` is `{ entryJs, initialJs, initialCss }` in gzipped kB. A metric with
 * no ceiling is reported and not gated — adding a ceiling is a deliberate act,
 * so a new metric cannot break the build the day it is introduced.
 */
export function evaluate(measured, budget) {
  const ceilings = budget.ceilingsKb ?? {}
  const targets = budget.targetsKb ?? {}
  const rows = Object.keys(measured).sort().map((metric) => {
    const value = measured[metric]
    const ceiling = ceilings[metric]
    const target = targets[metric]
    return {
      metric,
      value,
      ceiling: ceiling ?? null,
      target: target ?? null,
      // A metric nobody set a ceiling for cannot be over it.
      over: ceiling !== undefined && value > ceiling,
      // Headroom is what the ratchet gives back: a build that came in under its
      // ceiling can lower it, and `--update` is how that is banked.
      slackKb: ceiling === undefined ? null : Math.round((ceiling - value) * 10) / 10,
      meetsTarget: target === undefined ? null : value <= target,
    }
  })
  return { rows, breaches: rows.filter((r) => r.over), ok: rows.every((r) => !r.over) }
}

/**
 * The ratchet: ceilings only ever move DOWN.
 *
 * Same rule as `scripts/brand_ratchet.py`, for the same reason. A budget that
 * can be raised to make a build pass is not a budget, it is a comment — and the
 * raise always looks justified in the moment it is made. Lowering is automatic
 * because a shrink nobody banks is a shrink the next change silently spends.
 *
 * Ceilings sit a few percent ABOVE the measured size rather than exactly on it.
 * An exact ceiling fails on the first PR that adds a component — which teaches
 * everyone to bump the number reflexively, and a budget nobody believes is worse
 * than none. The headroom absorbs ordinary growth (and the byte-level drift
 * between zlib versions on a dev machine and on CI) while a 40kB regression
 * still fails loudly.
 */
export const DEFAULT_HEADROOM_PCT = 5

export function ratchet(budget, measured, { headroomPct = DEFAULT_HEADROOM_PCT } = {}) {
  const ceilings = { ...(budget.ceilingsKb ?? {}) }
  const lowered = []
  for (const [metric, value] of Object.entries(measured)) {
    const current = ceilings[metric]
    if (current === undefined) continue
    const next = Math.round(value * (1 + headroomPct / 100) * 10) / 10
    if (next < current) {
      ceilings[metric] = next
      lowered.push({ metric, from: current, to: next })
    }
  }
  return { budget: { ...budget, ceilingsKb: ceilings }, lowered }
}

export function formatReport({ rows }) {
  const header = 'metric        gzip kB    ceiling     target   status'
  const lines = rows.map((r) => {
    const status = r.over
      ? `OVER by ${Math.round((r.value - r.ceiling) * 10) / 10} kB`
      : r.meetsTarget === false
        ? `ok (target ${r.target} kB not yet met)`
        : 'ok'
    return [
      r.metric.padEnd(13),
      String(r.value).padStart(7),
      String(r.ceiling ?? '—').padStart(11),
      String(r.target ?? '—').padStart(11),
      `   ${status}`,
    ].join('')
  })
  return [header, ...lines].join('\n')
}
