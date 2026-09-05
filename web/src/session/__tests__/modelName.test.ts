// @vitest-environment node
import { describe, it, expect } from 'vitest'
import {
  modelName, modelTier, isGatedTier, tierLabel, modelFamily, engineLabel, engineBadge,
} from '../modelName'

describe('modelName', () => {
  it('maps known claude ids to friendly names', () => {
    expect(modelName('claude-opus-4-8')).toBe('Opus 4.8')
    expect(modelName('claude-haiku-4-5')).toBe('Haiku 4.5')
    expect(modelName('claude-sonnet-4-6')).toBe('Sonnet 4.6')
  })
  it('handles the Claude 5 family ids, which carry no minor version', () => {
    expect(modelName('claude-fable-5')).toBe('Fable 5')
    expect(modelName('claude-opus-5')).toBe('Opus 5')
    // A dated id still formats from its version prefix.
    expect(modelName('claude-fable-5-20260101')).toBe('Fable 5')
    expect(modelName('claude-haiku-4-5-20251001')).toBe('Haiku 4.5')
  })
  it('returns unknown ids verbatim', () => {
    expect(modelName('gpt-4o')).toBe('gpt-4o')
  })
  it('returns empty string for null/empty', () => {
    expect(modelName(null)).toBe('')
    expect(modelName('')).toBe('')
  })
})

describe('modelTier', () => {
  it('returns the tier for full claude model ids', () => {
    expect(modelTier('claude-fable-5')).toBe('fable')
    expect(modelTier('claude-opus-4-8')).toBe('opus')
    expect(modelTier('claude-sonnet-4-6')).toBe('sonnet')
    expect(modelTier('claude-haiku-4-5')).toBe('haiku')
  })

  it('handles context-size suffixes like [1m]', () => {
    expect(modelTier('claude-fable-5[1m]')).toBe('fable')
    expect(modelTier('fable[1m]')).toBe('fable')
  })

  it('handles bare short aliases', () => {
    expect(modelTier('fable')).toBe('fable')
    expect(modelTier('opus')).toBe('opus')
    expect(modelTier('sonnet')).toBe('sonnet')
    expect(modelTier('haiku')).toBe('haiku')
  })

  it('maps opusplan to opus', () => {
    expect(modelTier('opusplan')).toBe('opus')
  })

  it('returns null for default, unknown, and null/undefined', () => {
    expect(modelTier('default')).toBeNull()
    expect(modelTier('gpt-4o')).toBeNull()
    expect(modelTier(null)).toBeNull()
    expect(modelTier(undefined)).toBeNull()
    expect(modelTier('')).toBeNull()
  })
})

describe('isGatedTier', () => {
  it('returns true only for fable tier models', () => {
    expect(isGatedTier('fable')).toBe(true)
    expect(isGatedTier('claude-fable-5')).toBe(true)
    expect(isGatedTier('claude-fable-5[1m]')).toBe(true)
  })

  it('returns false for non-fable tiers', () => {
    expect(isGatedTier('opus')).toBe(false)
    expect(isGatedTier('claude-opus-4-8')).toBe(false)
    expect(isGatedTier('opusplan')).toBe(false)
    expect(isGatedTier('default')).toBe(false)
    expect(isGatedTier(null)).toBe(false)
    expect(isGatedTier(undefined)).toBe(false)
  })

  it('is false for a codex session — tier gating is a Claude model policy', () => {
    // The gate is `resolve_model_policy`'s tier match, which no OpenAI id can
    // hit; saying otherwise would tint a Codex card as "expensive tier".
    expect(isGatedTier('gpt-5.1-codex', 'codex')).toBe(false)
    expect(isGatedTier('fable', 'codex')).toBe(false)
  })
})

// ── engine awareness (Codex) ─────────────────────────────────────────────────

describe('modelFamily', () => {
  it('recognises the two engine families from the id shape', () => {
    expect(modelFamily('claude-opus-4-8')).toBe('claude')
    expect(modelFamily('opus')).toBe('claude')
    expect(modelFamily('gpt-5')).toBe('openai')
    expect(modelFamily('gpt-5.1-codex-mini')).toBe('openai')
  })

  it('is unknown for ids it cannot place', () => {
    expect(modelFamily('some-future-model')).toBe('unknown')
    expect(modelFamily(null)).toBe('unknown')
    expect(modelFamily('')).toBe('unknown')
  })
})

describe('modelName — codex models', () => {
  it('renders OpenAI ids the way the Codex catalog labels them', () => {
    // Server evidence: server/codex/catalog.py options label "gpt-5.1-codex"
    // as "GPT-5.1 Codex" (server/tests/test_codex_model_dispatch.py).
    expect(modelName('gpt-5')).toBe('GPT-5')
    expect(modelName('gpt-5-codex')).toBe('GPT-5 Codex')
    expect(modelName('gpt-5.1-codex')).toBe('GPT-5.1 Codex')
    expect(modelName('gpt-5.1-codex-mini')).toBe('GPT-5.1 Codex Mini')
  })

  it('takes the session engine as a hint without changing a Claude id', () => {
    expect(modelName('gpt-5-codex', 'codex')).toBe('GPT-5 Codex')
    expect(modelName('claude-opus-4-8', 'claude_code')).toBe('Opus 4.8')
    // An engine hint never invents a label for an id it cannot parse.
    expect(modelName('o3', 'codex')).toBe('o3')
  })
})

describe('tierLabel — codex models', () => {
  it('labels a codex model instead of falling through to the raw id', () => {
    expect(tierLabel('gpt-5.1-codex', 'codex')).toBe('GPT-5.1 Codex')
  })

  it('keeps the Claude behaviour', () => {
    expect(tierLabel('claude-opus-5')).toBe('Opus 5')
    expect(tierLabel('fable')).toBe('Fable')
    expect(tierLabel(null)).toBe('this model')
  })
})

describe('engineLabel / engineBadge', () => {
  it('labels every engine', () => {
    expect(engineLabel('claude_code')).toBe('Claude Code')
    expect(engineLabel('codex')).toBe('Codex')
    expect(engineLabel(undefined)).toBe('Claude Code')
  })

  it('badges only a non-default engine, so a Claude-only fleet gains no chip', () => {
    expect(engineBadge('codex')).toBe('Codex')
    expect(engineBadge('claude_code')).toBeNull()
    expect(engineBadge(undefined)).toBeNull()
  })
})
