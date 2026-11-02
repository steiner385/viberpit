import { describe, it, expect } from 'vitest'
import { TRANSCRIPT_VIEW_MODES, nextViewMode, resolveViewMode, viewModeLabel } from './viewMode'

describe('TRANSCRIPT_VIEW_MODES', () => {
  it('is Normal, Thinking, Verbose — in that order', () => {
    expect(TRANSCRIPT_VIEW_MODES.map((m) => m.label)).toEqual(['Normal', 'Thinking', 'Verbose'])
    expect(TRANSCRIPT_VIEW_MODES.map((m) => m.value)).toEqual(['normal', 'thinking', 'verbose'])
  })

  it('labels each mode', () => {
    expect(viewModeLabel('normal')).toBe('Normal')
    expect(viewModeLabel('thinking')).toBe('Thinking')
    expect(viewModeLabel('verbose')).toBe('Verbose')
  })
})

describe('nextViewMode', () => {
  it('cycles normal → thinking → verbose → normal', () => {
    expect(nextViewMode('normal')).toBe('thinking')
    expect(nextViewMode('thinking')).toBe('verbose')
    expect(nextViewMode('verbose')).toBe('normal')
  })
})

describe('resolveViewMode', () => {
  const S = { defaultToolDetail: 'auto' as const, thinkingCollapsed: true }

  it('Normal follows the settings exactly', () => {
    expect(resolveViewMode('normal', S)).toEqual({ toolDetailMode: 'auto', thinkingCollapsed: true })
  })

  it('Normal follows S when S = {all-closed, false}', () => {
    expect(resolveViewMode('normal', { defaultToolDetail: 'all-closed', thinkingCollapsed: false }))
      .toEqual({ toolDetailMode: 'all-closed', thinkingCollapsed: false })
  })

  it('Thinking keeps the tool detail setting and opens thinking', () => {
    expect(resolveViewMode('thinking', S)).toEqual({ toolDetailMode: 'auto', thinkingCollapsed: false })
    expect(resolveViewMode('thinking', { defaultToolDetail: 'all-closed', thinkingCollapsed: true }))
      .toEqual({ toolDetailMode: 'all-closed', thinkingCollapsed: false })
  })

  it('Verbose opens every tool result and every thinking block', () => {
    expect(resolveViewMode('verbose', S)).toEqual({ toolDetailMode: 'all-open', thinkingCollapsed: false })
    expect(resolveViewMode('verbose', { defaultToolDetail: 'all-closed', thinkingCollapsed: true }))
      .toEqual({ toolDetailMode: 'all-open', thinkingCollapsed: false })
  })
})
