// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { PERMISSION_MODES, SESSION_MODE_OPTIONS, modeLabel } from '../permissionModes'

describe('PERMISSION_MODES', () => {
  const values = PERMISSION_MODES.map((m) => m.value)

  it('exposes acceptEdits and bypassPermissions (issue #750)', () => {
    // These mirror the SDK's own permission_mode names and must stay in sync
    // with the backend allowlist in server/api/permission_profiles.py.
    expect(values).toContain('acceptEdits')
    expect(values).toContain('bypassPermissions')
  })

  it('every mode has a non-empty human label', () => {
    for (const { value, label } of PERMISSION_MODES) {
      expect(label, `mode ${value} needs a label`).toBeTruthy()
    }
  })

  it('has no duplicate values', () => {
    expect(new Set(values).size).toBe(values.length)
  })
})

describe('SESSION_MODE_OPTIONS', () => {
  it('lists the Desktop modes first, then the ViberPit ones, in order', () => {
    expect(SESSION_MODE_OPTIONS.map((o) => o.label)).toEqual([
      'Ask', 'Accept edits', 'Plan', 'Auto', 'Bypass', 'Read-only allowlist', 'Full auto',
    ])
    expect(SESSION_MODE_OPTIONS.map((o) => o.group)).toEqual([
      'desktop', 'desktop', 'desktop', 'desktop', 'desktop', 'viberpit', 'viberpit',
    ])
  })

  it('keeps the mode keys unchanged', () => {
    expect(SESSION_MODE_OPTIONS.map((o) => o.value)).toEqual([
      'interactive', 'acceptEdits', 'plan', 'auto', 'bypassPermissions', 'allowlist', 'full-auto',
    ])
  })

  it('contains every settable default, which excludes plan', () => {
    const all = SESSION_MODE_OPTIONS.map((o) => o.value as string)
    for (const m of PERMISSION_MODES) expect(all).toContain(m.value)
    expect(PERMISSION_MODES.map((m) => m.value as string)).not.toContain('plan')
  })
})

describe('modeLabel', () => {
  it('maps keys to labels', () => {
    expect(modeLabel('full-auto')).toBe('Full auto')
  })
  it('reads the SDK plan-exit sentinel as Ask', () => {
    expect(modeLabel('default')).toBe('Ask')
  })
  it('returns an unknown key unchanged', () => {
    expect(modeLabel('weird')).toBe('weird')
  })
})
