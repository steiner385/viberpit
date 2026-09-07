import { describe, it, expect, beforeEach } from 'vitest'
import { render, renderHook } from '@testing-library/react'
import { setBaseTitle, releaseBaseTitle, setNeedsYouCount, resetDocumentTitle, APP_NAME } from './documentTitle'
import { PageHeader } from '@/ui/PageHeader'
import { useNeedsYouTitle } from '@/hooks/useNeedsYouTitle'
import type { Session } from '@/api/types'

describe('documentTitle', () => {
  beforeEach(() => { resetDocumentTitle() })

  it('is just the app name with no inputs', () => {
    expect(document.title).toBe(APP_NAME)
  })

  it('appends the app name to a base title', () => {
    const t = Symbol('t')
    setBaseTitle(t, 'Sessions')
    expect(document.title).toBe('Sessions — ViberPit')
  })

  it('prefixes the needs-you count onto a base title', () => {
    const t = Symbol('t')
    setBaseTitle(t, 'Sessions')
    setNeedsYouCount(3)
    expect(document.title).toBe('(3) Sessions — ViberPit')
  })

  it('uses the legacy needs-you string when no base title is set', () => {
    setNeedsYouCount(2)
    expect(document.title).toBe('(2) ViberPit — needs you')
  })

  it('order of the two inputs does not matter', () => {
    const t = Symbol('t')
    setNeedsYouCount(3)
    setBaseTitle(t, 'Pipeline')
    expect(document.title).toBe('(3) Pipeline — ViberPit')
  })

  it('clearing the count restores the plain base title', () => {
    const t = Symbol('t')
    setBaseTitle(t, 'Sessions')
    setNeedsYouCount(3)
    setNeedsYouCount(0)
    expect(document.title).toBe('Sessions — ViberPit')
  })

  it('releasing the base title falls back to the app name', () => {
    const t = Symbol('t')
    setBaseTitle(t, 'Sessions')
    releaseBaseTitle(t)
    expect(document.title).toBe(APP_NAME)
  })

  it("a later base title from the same token replaces its own earlier one", () => {
    const t = Symbol('t')
    setBaseTitle(t, 'Sessions')
    setBaseTitle(t, 'Settings')
    expect(document.title).toBe('Settings — ViberPit')
  })

  it('an older token releasing while a newer token is on top leaves the newer title alone', () => {
    const older = Symbol('older')
    const newer = Symbol('newer')
    setBaseTitle(older, 'Alpha')
    setBaseTitle(newer, 'Beta')
    releaseBaseTitle(older)
    expect(document.title).toBe('Beta — ViberPit')
  })

  it('a newer token releasing while an older token is still claimed restores the older title', () => {
    const older = Symbol('older')
    const newer = Symbol('newer')
    setBaseTitle(older, 'Alpha')
    setBaseTitle(newer, 'Beta')
    releaseBaseTitle(newer)
    expect(document.title).toBe('Alpha — ViberPit')
  })

  it('PageHeader and useNeedsYouTitle compose instead of clobbering', () => {
    render(<PageHeader title="Sessions" />)
    expect(document.title).toBe('Sessions — ViberPit')
    // A real minimal Session whose own status genuinely triages to
    // 'needs-you' (see session/statusTier.ts's PRESENTATION table) — not a
    // cast. An unsafe cast in a test is tracked debt here (issue #952); this
    // fixture is cheap enough to construct for real instead of adding to it.
    const needsYouSession: Session = {
      id: 'a',
      name: 'a',
      accountName: 'acct',
      engine: 'claude_code',
      engineSessionId: null,
      status: 'waiting_input',
      createdAt: '2026-01-01T00:00:00Z',
      prs: [],
    }
    renderHook(() => useNeedsYouTitle([needsYouSession]))
    expect(document.title).toBe('(1) Sessions — ViberPit')
  })
})
