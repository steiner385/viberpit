import { describe, it, expect } from 'vitest'
import { buildManifest } from '../manifest'
import { BRAND_NAME } from '@/brand'

describe('buildManifest', () => {
  it('names the app from the brand single source of truth', () => {
    const m = buildManifest()
    expect(m.name).toBe(BRAND_NAME)
    expect(m.short_name).toBe(BRAND_NAME)
  })

  it('is installable: standalone display, start_url, and a pinned id', () => {
    const m = buildManifest()
    expect(m.display).toBe('standalone')
    expect(m.start_url).toBe('/')
    // Pins app identity so a later start_url change updates the installed app
    // instead of installing a second one.
    expect(m.id).toBe('/')
  })

  it('ships both icon sizes browsers require, plus a maskable variant', () => {
    const icons = buildManifest().icons
    expect(icons.map((i) => i.sizes)).toEqual(
      expect.arrayContaining(['192x192', '512x512']),
    )
    expect(icons.every((i) => i.type === 'image/png')).toBe(true)
    expect(icons.some((i) => i.purpose === 'maskable')).toBe(true)
    expect(icons.every((i) => i.src.startsWith('/icons/'))).toBe(true)
  })

  it('matches the app background so the splash screen does not flash', () => {
    const m = buildManifest()
    expect(m.background_color).toBe('#16171d')
    expect(m.theme_color).toBe('#16171d')
  })
})
