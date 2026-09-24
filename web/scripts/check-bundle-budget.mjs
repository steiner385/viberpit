#!/usr/bin/env node
/**
 * Enforce `web/bundle-budget.json` against a built `web/dist` (#1411).
 *
 *   npm run build && npm run budget      # gate
 *   npm run budget -- --update           # bank a shrink (ceilings only go down)
 *
 * Run by CI's `frontend` job straight after the build, so a change that puts
 * 40kB back on the critical path fails on the PR that did it rather than
 * surfacing three months later as "the app feels slow".
 *
 * The rules live in `bundleBudget.mjs`; this file is fs + gzip + exit code.
 */
import { gzipSync } from 'node:zlib'
import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { evaluate, formatReport, parseInitialAssets, ratchet, toKb } from './bundleBudget.mjs'

const WEB_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')
const DIST = join(WEB_ROOT, 'dist')
const BUDGET_PATH = join(WEB_ROOT, 'bundle-budget.json')

/** Default zlib level — what a stock nginx/uvicorn gzip actually emits, so the
 *  number in CI is the number on the wire. Brotli would be smaller and is what
 *  most browsers negotiate; gzip is the conservative, universally-available
 *  floor, and a budget wants the worst realistic case. */
function gzippedKb(paths) {
  let bytes = 0
  for (const p of paths) {
    const file = join(DIST, p.replace(/^\//, ''))
    if (!existsSync(file)) {
      console.error(`bundle-budget: ${p} is referenced by index.html but missing from dist/`)
      process.exit(2)
    }
    bytes += gzipSync(readFileSync(file)).length
  }
  return toKb(bytes)
}

function main() {
  const update = process.argv.includes('--update')
  const indexHtml = join(DIST, 'index.html')
  if (!existsSync(indexHtml)) {
    console.error('bundle-budget: no dist/index.html — run `npm run build` first.')
    process.exit(2)
  }

  const { scripts, modulepreloads, stylesheets } = parseInitialAssets(readFileSync(indexHtml, 'utf8'))
  if (scripts.length === 0) {
    console.error('bundle-budget: index.html references no scripts — refusing to report a pass.')
    process.exit(2)
  }

  const measured = {
    entryJs: gzippedKb(scripts),
    initialJs: gzippedKb([...scripts, ...modulepreloads]),
    initialCss: gzippedKb(stylesheets),
  }

  const budget = JSON.parse(readFileSync(BUDGET_PATH, 'utf8'))
  const result = evaluate(measured, budget)

  console.log(`bundle budget — ${scripts.length} entry script(s), `
    + `${modulepreloads.length} preloaded chunk(s), ${stylesheets.length} stylesheet(s)`)
  console.log(formatReport(result))

  if (update) {
    const { budget: next, lowered } = ratchet(budget, measured)
    if (lowered.length === 0) {
      console.log('\nnothing to bank — no metric came in under its ceiling.')
    } else {
      writeFileSync(BUDGET_PATH, `${JSON.stringify(next, null, 2)}\n`)
      for (const l of lowered) console.log(`\nlowered ${l.metric}: ${l.from} → ${l.to} kB`)
    }
    // --update is a maintenance action, never a gate: exit on the measurement,
    // not on what was banked.
  }

  if (!result.ok) {
    console.error('\nOver budget. Either split the new weight off the critical path, or — if the '
      + 'growth is genuinely load-bearing — raise the ceiling in web/bundle-budget.json in the '
      + 'same PR, with the reason in the commit message.')
    process.exit(1)
  }
  console.log('\nwithin budget.')
}

main()
