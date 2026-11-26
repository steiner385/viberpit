#!/usr/bin/env node
/**
 * Measure the E2E flake rate from Playwright JSON reports (#1412, G14).
 *
 *   npx playwright test --reporter=json > run1.json   # or CI's test-results/e2e.json
 *   node scripts/check-e2e-flake.mjs run1.json run2.json …
 *   node scripts/check-e2e-flake.mjs --strict reports/*.json   # exit 1 unless within budget
 *
 * Reports from many runs are pooled: that is how a <1% budget gets enough samples
 * to mean anything (see MIN_RUNS_FOR_VERDICT). Default exit is 0 so the nightly can
 * publish the number without going red; `--strict` is what a promotion decision uses.
 *
 * Exit: 0 ok · 1 (--strict) not `within` budget · 2 bad input.
 * The rules live in `e2eFlake.mjs`; this file is fs + exit code.
 */
import { readFileSync } from 'node:fs'

import { formatReport, parseReport, tally, verdict } from './e2eFlake.mjs'

function main() {
  const args = process.argv.slice(2)
  const strict = args.includes('--strict')
  const files = args.filter((a) => !a.startsWith('--'))
  if (files.length === 0) {
    console.error('check-e2e-flake: pass at least one Playwright JSON report.')
    process.exit(2)
  }

  const entries = []
  for (const file of files) {
    try {
      entries.push(...parseReport(JSON.parse(readFileSync(file, 'utf8'))))
    } catch (err) {
      console.error(`check-e2e-flake: cannot read ${file}: ${err.message}`)
      process.exit(2)
    }
  }

  const result = tally(entries)
  console.log(formatReport(result))
  // Every engine must clear the budget on its own: a clean Chromium pooled with a flaky
  // WebKit would average the problem away, and promotion is per-engine in effect.
  const buckets = [...Object.values(result.byProject), result.total]
  if (strict && !buckets.every((b) => verdict(b) === 'within')) process.exit(1)
}

main()
