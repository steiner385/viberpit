import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import FastToggle from './FastToggle'
import { FAST_MODE_COPY, fastModeLiveNote } from './fastMode'

describe('fastModeLiveNote', () => {
  it('says nothing while fast mode is off or agrees', () => {
    expect(fastModeLiveNote(false, 'cooldown', null)).toBeNull()
    expect(fastModeLiveNote(true, 'on', null)).toBeNull()
    expect(fastModeLiveNote(true, undefined, null)).toBeNull()
  })
  it('names the cooldown and the CLI-disabled reason', () => {
    expect(fastModeLiveNote(true, 'cooldown', null)).toMatch(/Cooling down/)
    expect(fastModeLiveNote(true, 'off', 'out_of_credits')).toMatch(/out of credits/)
    expect(fastModeLiveNote(true, 'off', null)).toMatch(/Disabled/)
  })
})

describe('FastToggle live state', () => {
  it('keeps the cost copy as the tooltip when live state is normal', () => {
    render(<FastToggle on onToggle={vi.fn()} liveState="on" />)
    expect(screen.getByRole('button', { name: 'Fast mode' }).getAttribute('title')).toBe(FAST_MODE_COPY)
  })
  it('leads the tooltip with the cooldown', () => {
    render(<FastToggle on onToggle={vi.fn()} liveState="cooldown" />)
    const b = screen.getByRole('button', { name: 'Fast mode' })
    expect(b.getAttribute('title')).toMatch(/^Cooling down/)
    expect(b.getAttribute('title')).toContain(FAST_MODE_COPY)
  })
  it('shows the disabled reason', () => {
    render(<FastToggle on onToggle={vi.fn()} liveState="off" disabledReason="out_of_credits" />)
    expect(screen.getByRole('button', { name: 'Fast mode' }).getAttribute('title')).toMatch(/out of credits/)
  })
})
