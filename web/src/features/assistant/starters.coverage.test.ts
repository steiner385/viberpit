import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { STARTERS } from './starters'

// Mirror of server/tests/test_assistant_conformance.py::test_every_starter_has_an_eval_case,
// so a chip added on the web side fails the web gate too, not only the server gate.
//
// The five G1 chips this test was born tracking are closed: Task 5.3 backed the insights
// chip with list_stage_models, Task 5.6 replaced the initiatives/projects chips no tool
// could serve. The tracking array is deleted rather than left empty — an empty allow-list
// is the same assertion with a stale comment attached.
describe('starter chips are backed by eval cases', () => {
  it('every chip string appears as a prompt in cases.jsonl', () => {
    const cases = readFileSync('../docs/superpowers/evals/assistant/cases.jsonl', 'utf8')
      .split('\n').filter(Boolean).map((l) => JSON.parse(l) as { prompt: string })
    const prompts = new Set(cases.map((c) => c.prompt))
    const chips = new Set(Object.values(STARTERS).flat())
    const missing = [...chips].filter((c) => !prompts.has(c)).sort()
    expect(missing).toEqual([])
  })
})
