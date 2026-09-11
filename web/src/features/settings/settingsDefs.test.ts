import { describe, it, expect } from 'vitest'
import {
  SETTINGS_DEFS, SETTINGS_BY_ID, SETTINGS_GROUPS, DEFAULT_SETTINGS_SECTION,
  isSettingsSectionId, sectionsInGroup,
} from './settingsDefs'
import { readLastSection, writeLastSection, LAST_SECTION_KEY } from './lastSection'

describe('settingsDefs', () => {
  it('has 17 sections in 5 groups, ids unique and URL-safe, every def described', () => {
    expect(SETTINGS_DEFS).toHaveLength(17)
    expect(SETTINGS_GROUPS.map((g) => g.id)).toEqual(['workspace', 'sessions', 'accounts', 'claude', 'about'])
    const ids = SETTINGS_DEFS.map((d) => d.id)
    expect(new Set(ids).size).toBe(ids.length)
    for (const d of SETTINGS_DEFS) {
      expect(d.id).toMatch(/^[a-z]+$/)
      expect(d.description.length).toBeGreaterThan(10)
      expect(SETTINGS_GROUPS.some((g) => g.id === d.group)).toBe(true)
    }
  })

  it('keeps every legacy section id so old links resolve (spec §5)', () => {
    for (const legacy of ['general', 'accounts', 'notifications', 'hooks', 'plugins', 'skills', 'commands', 'mcp', 'presets', 'templates', 'profiles', 'snapshots', 'fleet']) {
      expect(isSettingsSectionId(legacy)).toBe(true)
    }
    expect(isSettingsSectionId('')).toBe(false)
    // R1/task 6: the Automations section is retired — its controls (enable
    // switch, mode select, config form, schedule editor) moved to the Delivery
    // detail at `/delivery`, so `automation` is no longer a section id at all.
    expect(isSettingsSectionId('automation')).toBe(false)
    expect(DEFAULT_SETTINGS_SECTION).toBe('general')
  })

  it('ownsChassis marks exactly the five sections that mount their own chassis', () => {
    // The flag is what `SettingsPage` branches on, so it is also the list of
    // sections NOT wrapped in `HostedSection`. PRs 5b–5e each add an id here in
    // the same change that migrates the section. `deploy` was born with it.
    expect(SETTINGS_DEFS.filter((d) => d.ownsChassis).map((d) => d.id))
      .toEqual(['general', 'appearance', 'sessions', 'deploy', 'about'])
  })

  it('groups sections in nav order; Accounts carries the attention badge', () => {
    expect(sectionsInGroup('workspace').map((d) => d.id)).toEqual(['general', 'appearance', 'notifications'])
    expect(sectionsInGroup('sessions').map((d) => d.id)).toEqual(['sessions', 'presets', 'templates', 'profiles'])
    expect(sectionsInGroup('about').map((d) => d.id)).toEqual(['deploy', 'about'])
    expect(SETTINGS_BY_ID.accounts.badge).toBe('accountsAttention')
    expect(SETTINGS_BY_ID.appearance.scope).toBe('browser')
    expect(SETTINGS_BY_ID.about.scope).toBe('none')
  })

  // Settings › Deploy is the phone's only deploy surface, since
  // `shell/useAppBanners` gives the deploy banner `hideOn: ['phone']`. Its badge
  // is what makes it findable there, so the pairing is pinned rather than left
  // to whoever edits the registry next.
  it('Deploy carries the pending-count badge that stands in for the hidden banner', () => {
    expect(SETTINGS_BY_ID.deploy.badge).toBe('deployPending')
    expect(SETTINGS_BY_ID.deploy.scope).toBe('server')
  })
})

describe('lastSection', () => {
  it('round-trips a valid id and ignores garbage', () => {
    expect(readLastSection()).toBeNull()
    writeLastSection('fleet')
    expect(readLastSection()).toBe('fleet')
    localStorage.setItem(LAST_SECTION_KEY, 'nope')
    expect(readLastSection()).toBeNull()
  })
})
