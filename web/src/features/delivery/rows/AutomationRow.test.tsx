import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { Automation } from '@/api/automations'
import { ViewportProvider } from '@/shell/useViewport'
import { AUTOMATIONS } from '../__fixtures__/automations'
import { AutomationRow } from './AutomationRow'

const runner = AUTOMATIONS[1]! // actionableCount: 2
const mount = (a: Automation) => render(
  <ViewportProvider kiosk={null}><AutomationRow automation={a} selected={false} onOpen={vi.fn()} /></ViewportProvider>)

describe('AutomationRow', () => {
  it('an actionable count wears the accent badge', () => {
    mount(runner)
    const badge = screen.getByTitle('Actionable items')
    expect(badge).toHaveTextContent('2')
    expect(badge.className).toMatch(/text-accent/)
    expect(screen.queryByTitle('Parked items')).toBeNull()
  })
  // #2936: a paused automation's decisions are parked — a neutral count, never
  // the accent/attention one, and the tier line says why.
  it('a paused automation\'s parked items wear a neutral badge, not the actionable one', () => {
    const paused = { ...runner, enabled: false, paused: true, actionableCount: 0, parkedCount: 50 }
    mount(paused)
    expect(screen.queryByTitle('Actionable items')).toBeNull()
    const badge = screen.getByTitle('Parked items')
    expect(badge).toHaveTextContent('50')
    expect(badge.className).toMatch(/text-ink-muted/)
    expect(badge.className).not.toMatch(/accent|warning/)
    expect(screen.getByText(/Paused · 50 parked/)).toBeInTheDocument()
  })
})
