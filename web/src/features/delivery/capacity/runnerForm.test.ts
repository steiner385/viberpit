import { describe, it, expect } from 'vitest'
import { CAPACITY } from './__fixtures__/capacity'
import { PIN_KEY, RUNNER_KEYS, parseRunnerDraft, seedCapacityDraft } from './runnerForm'

const CURRENT = { ceiling: 5, budget: 10, perAccount: 2, globalCap: 20 }

describe('seedCapacityDraft (spec 2026-09-20 §5.2 item 3)', () => {
  it('namespaces pins and seeds the runner and governor fields from their configs', () => {
    const d = seedCapacityDraft(CAPACITY, { max_concurrent: 5, max_spawns_per_day: 10 }, { sessions_per_account: 2 })
    expect(d[PIN_KEY('kindash')]).toBe('2')
    expect(d[RUNNER_KEYS.ceiling]).toBe('5')
    expect(d[RUNNER_KEYS.budget]).toBe('10')
    expect(d[RUNNER_KEYS.perAccount]).toBe('2')
  })
  it('falls back to the readout ceiling and blanks when an automation is not registered', () => {
    const d = seedCapacityDraft(CAPACITY, null, null)
    expect(d[RUNNER_KEYS.ceiling]).toBe(String(CAPACITY.runner.ceiling))
    expect(d[RUNNER_KEYS.budget]).toBe('')
    expect(d[RUNNER_KEYS.perAccount]).toBe('')
  })
})

describe('parseRunnerDraft', () => {
  const base = { [RUNNER_KEYS.ceiling]: '5', [RUNNER_KEYS.budget]: '10', [RUNNER_KEYS.perAccount]: '2' }
  it('unchanged values produce no patches', () => {
    expect(parseRunnerDraft(base, CURRENT)).toEqual({ runner: null, governor: null, ceilingRaised: false })
  })
  it('a lowered ceiling and a changed budget patch the runner; per-account patches the governor', () => {
    expect(parseRunnerDraft({ ...base, [RUNNER_KEYS.ceiling]: '4', [RUNNER_KEYS.budget]: '12', [RUNNER_KEYS.perAccount]: '3' }, CURRENT))
      .toEqual({ runner: { max_concurrent: 4, max_spawns_per_day: 12 }, governor: { sessions_per_account: 3 }, ceilingRaised: false })
  })
  it('a raised ceiling is flagged for confirmation', () => {
    expect(parseRunnerDraft({ ...base, [RUNNER_KEYS.ceiling]: '8' }, CURRENT).ceilingRaised).toBe(true)
  })
  it('refuses a ceiling above the global cap, naming the cap', () => {
    expect(() => parseRunnerDraft({ ...base, [RUNNER_KEYS.ceiling]: '21' }, CURRENT))
      .toThrow('Runner ceiling must not exceed the global cap (20)')
  })
  it('refuses non-integers, negatives, a blank ceiling, and sessions per account below 1', () => {
    expect(() => parseRunnerDraft({ ...base, [RUNNER_KEYS.ceiling]: '' }, CURRENT)).toThrow('Runner ceiling must be a whole number ≥ 0')
    expect(() => parseRunnerDraft({ ...base, [RUNNER_KEYS.ceiling]: '2.5' }, CURRENT)).toThrow('Runner ceiling must be a whole number ≥ 0')
    expect(() => parseRunnerDraft({ ...base, [RUNNER_KEYS.budget]: '-1' }, CURRENT)).toThrow('Daily budget must be a whole number ≥ 0')
    expect(() => parseRunnerDraft({ ...base, [RUNNER_KEYS.perAccount]: '0' }, CURRENT)).toThrow('Sessions per account must be a whole number ≥ 1')
  })
  it('ignores a blank budget or per-account when the automation is not registered (no current value)', () => {
    expect(parseRunnerDraft({ ...base, [RUNNER_KEYS.budget]: '', [RUNNER_KEYS.perAccount]: '' }, { ...CURRENT, budget: null, perAccount: null }))
      .toEqual({ runner: null, governor: null, ceilingRaised: false })
  })
  it('a registered automation whose row never stored the key still takes a typed value', () => {
    expect(parseRunnerDraft({ ...base, [RUNNER_KEYS.budget]: '12', [RUNNER_KEYS.perAccount]: '3' }, { ...CURRENT, budget: null, perAccount: null }))
      .toEqual({ runner: { max_spawns_per_day: 12 }, governor: { sessions_per_account: 3 }, ceilingRaised: false })
  })
})
