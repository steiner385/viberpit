import { describe, expect, it } from 'vitest'
import type { AgentCapabilities, ModelOption } from '@/api/types'
import { effortChoices, effortStep } from './effortChoices'

const CLAUDE = { reasoningEffort: false, flagSettings: true } as AgentCapabilities
const CODEX = { reasoningEffort: true, flagSettings: false } as AgentCapabilities
const CODEX_MODELS: ModelOption[] = [
  { id: 'gpt-5', label: 'GPT-5', default: true, reasoningEfforts: ['minimal', 'low', 'medium', 'high'], defaultReasoningEffort: 'medium' },
]

describe('effortChoices', () => {
  it("reads a Codex model's catalog efforts and its default effort", () => {
    expect(effortChoices(null, { engine: 'codex' }, CODEX, CODEX_MODELS))
      .toEqual({ efforts: ['minimal', 'low', 'medium', 'high'], effort: 'medium' })
  })

  it("reads a Claude session's reported levels, and the session's own effort", () => {
    expect(effortChoices('opus', { supportedEffortLevels: ['low', 'medium', 'high'], reasoningEffort: 'high' }, CLAUDE, []))
      .toEqual({ efforts: ['low', 'medium', 'high'], effort: 'high' })
  })

  it('offers nothing to an engine that does not report its levels', () => {
    expect(effortChoices('opus', { supportedEffortLevels: ['low'] }, { ...CLAUDE, flagSettings: false }, []).efforts).toEqual([])
  })
})

describe('effortStep', () => {
  it('a known level steps one down', () => {
    expect(effortStep(['low', 'medium', 'high'], 'high')).toEqual({ to: 'medium' })
    expect(effortStep(['low', 'medium', 'high'], 'medium')).toEqual({ to: 'low' })
  })

  it('is null at the floor or with no levels', () => {
    expect(effortStep(['low', 'medium', 'high'], 'low')).toBeNull()
    expect(effortStep([], 'high')).toBeNull()
    expect(effortStep([], null)).toBeNull()
  })

  it("an unknown level with levels offered is 'pick' — open the picker, never guess", () => {
    expect(effortStep(['low', 'high'], null)).toBe('pick')
    expect(effortStep(['low', 'high'], 'max')).toBe('pick')
  })
})
