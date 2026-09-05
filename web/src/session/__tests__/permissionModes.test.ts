// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { PERMISSION_MODES } from '../permissionModes'

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
