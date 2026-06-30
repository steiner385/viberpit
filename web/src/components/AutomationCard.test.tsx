// web/src/components/AutomationCard.test.tsx
import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { AutomationCard } from './AutomationCard'
import * as hooks from '@/api/hooks'
import type { Automation } from '@/api/automations'

const PR_DOCTOR: Automation = {
  id: 'pr-doctor', name: 'PR Doctor', description: 'watches CI',
  enabled: false, mode: 'propose', modes: ['off', 'auto', 'propose'],
  status: { text: 'idle', level: 'idle', counts: null },
  configSchema: [], config: {}, lastActiveAt: null,
}

describe('AutomationCard', () => {
  beforeEach(() => vi.restoreAllMocks())

  it('renders name, status, and a toggle', () => {
    vi.spyOn(hooks, 'usePatchAutomation').mockReturnValue(
      { mutate: vi.fn() } as unknown as ReturnType<typeof hooks.usePatchAutomation>)
    vi.spyOn(hooks, 'useAutomationEvents').mockReturnValue(
      { data: [] } as unknown as ReturnType<typeof hooks.useAutomationEvents>)
    render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
    expect(screen.getByText('PR Doctor')).toBeInTheDocument()
    expect(screen.getByText('idle')).toBeInTheDocument()
    expect(screen.getByRole('switch')).toBeInTheDocument()
  })

  it('toggling enabled calls patch', () => {
    const mutate = vi.fn()
    vi.spyOn(hooks, 'usePatchAutomation').mockReturnValue(
      { mutate } as unknown as ReturnType<typeof hooks.usePatchAutomation>)
    vi.spyOn(hooks, 'useAutomationEvents').mockReturnValue(
      { data: [] } as unknown as ReturnType<typeof hooks.useAutomationEvents>)
    render(<MemoryRouter><AutomationCard automation={PR_DOCTOR} /></MemoryRouter>)
    fireEvent.click(screen.getByRole('switch'))
    expect(mutate).toHaveBeenCalledWith({ id: 'pr-doctor', body: { enabled: true } })
  })
})
