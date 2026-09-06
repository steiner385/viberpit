import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { STARTERS } from './starters'

// Mirror of server/tests/test_assistant_conformance.py::test_every_starter_has_an_eval_case,
// so a chip added on the web side fails the web gate too, not only the server gate.
//
// Three chips have no backing tool yet (spec v2 G1, tracked in the server test's
// KNOWN_STARTER_GAPS) — asserted here by exact equality so this test still catches a
// genuinely NEW gap while it stays green until Task 5.3/6.5 land the tools that back them.
const KNOWN_STARTER_GAPS = [
  'Which project has the most open work?',
  'What is blocked by a dependency?',
  'What regressed in CI recently?',
].sort()

describe('starter chips are backed by eval cases', () => {
  it('every chip with no eval case is a tracked G1 gap, and no more', () => {
    const cases = readFileSync('../docs/superpowers/evals/assistant/cases.jsonl', 'utf8')
      .split('\n').filter(Boolean).map((l) => JSON.parse(l) as { prompt: string })
    const prompts = new Set(cases.map((c) => c.prompt))
    const chips = new Set(Object.values(STARTERS).flat())
    const missing = [...chips].filter((c) => !prompts.has(c)).sort()
    expect(missing).toEqual(KNOWN_STARTER_GAPS)
  })
})
