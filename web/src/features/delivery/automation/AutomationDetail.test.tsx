import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AUTOMATIONS } from '../__fixtures__/automations'
import { AutomationDetail } from './AutomationDetail'
import { AutomationControls } from './AutomationControls'

const patch = vi.fn(); const trigger = vi.fn().mockResolvedValue({ status: 'done', level: 'ok', message: 'Fixed' })
vi.mock('@/api/hooks', () => ({
  usePatchAutomation: () => ({ mutate: patch, isPending: false }),
  useAutomationEvents: () => ({ data: [{ ts: '2026-09-11T10:00:00Z', level: 'info', kind: null, summary: 'scanned 3 PRs', target: null, sessionId: 's1' }] }),
  useAutomationSessions: () => ({ data: [{ id: 's1', name: 'fix #1', status: 'in_progress', originRef: null }, { id: 's2', name: 'old', status: 'ended', originRef: null }] }),
  useAutomationActionable: () => ({ data: [{ id: 'o/r#1', title: 'CI red on #1', actions: [{ kind: 'fix', label: 'Fix', style: 'safe' }] }] }),
  useAutomationTrigger: () => ({ mutateAsync: trigger, isPending: false }),
  useGithubRepos: () => ({ data: [{ nameWithOwner: 'o/r' }], isLoading: false }),
  useConsolidation: () => ({ data: undefined }), useConsolidate: () => ({ mutate: vi.fn(), isPending: false }),
}))
vi.mock('./ConsolidatePanel', () => ({ ConsolidatePanel: () => <div data-testid="consolidate" /> }))
vi.mock('./BacklogGroupsPanel', () => ({ BacklogGroupsPanel: () => <div data-testid="groups" /> }))
vi.mock('./ManualDispatchDialog', () => ({ ManualDispatchDialog: () => null }))

const prDoctor = AUTOMATIONS[0]
const mount = (a = prDoctor, path = '/delivery/automations/pr-doctor') => {
  const onOpenSession = vi.fn()
  render(<MemoryRouter initialEntries={[path]}><AutomationDetail automation={a} onOpenSession={onOpenSession} /></MemoryRouter>)
  return { onOpenSession }
}

describe('AutomationDetail', () => {
  beforeEach(() => { patch.mockReset(); trigger.mockClear() })
  it('Status tab: actionable items with their actions, and the pr-doctor consolidation panel', async () => {
    mount()
    expect(screen.getByRole('tab', { name: 'Status', selected: true })).toBeInTheDocument()
    expect(screen.getByText('CI red on #1')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Fix' }))
    await waitFor(() => expect(trigger).toHaveBeenCalledWith({ item_id: 'o/r#1', kind: 'fix' }))
    expect(screen.getByTestId('consolidate')).toBeInTheDocument()
  })
  it('tabs are URL state', () => {
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=events')
    expect(screen.getByRole('tab', { name: 'Events', selected: true })).toBeInTheDocument()
    expect(screen.getByText('scanned 3 PRs')).toBeInTheDocument()
  })
  it('Sessions tab links live sessions by work class and ended ones to the archive', () => {
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=sessions')
    expect(screen.getByRole('link', { name: /fix #1/ })).toHaveAttribute('href', '/sessions/background/s1')
    expect(screen.getByRole('link', { name: /old/ })).toHaveAttribute('href', '/sessions/archived/s2')
  })
  it('Config tab renders the schema and patches on change', () => {
    mount(prDoctor, '/delivery/automations/pr-doctor?tab=config')
    fireEvent.click(screen.getByRole('checkbox', { name: 'Auto-fix' }))
    expect(patch).toHaveBeenCalledWith({ id: 'pr-doctor', body: { config: expect.objectContaining({ auto_fix: false }) } })
  })
  it('an operator-triggered automation has no Schedule tab and no switch', () => {
    mount(AUTOMATIONS[2], '/delivery/automations/pm-classify')
    expect(screen.queryByRole('tab', { name: 'Schedule' })).not.toBeInTheDocument()
    render(<MemoryRouter><AutomationControls automation={AUTOMATIONS[2]} /></MemoryRouter>)
    expect(screen.queryByRole('switch')).not.toBeInTheDocument()
  })
  it('controls: mode select and enable switch patch the automation', () => {
    render(<MemoryRouter><AutomationControls automation={prDoctor} /></MemoryRouter>)
    fireEvent.change(screen.getByRole('combobox', { name: 'Mode' }), { target: { value: 'propose' } })
    expect(patch).toHaveBeenCalledWith({ id: 'pr-doctor', body: { mode: 'propose' } })
    fireEvent.click(screen.getByRole('switch', { name: 'PR doctor enabled' }))
    expect(patch).toHaveBeenCalledWith({ id: 'pr-doctor', body: { enabled: false } })
  })
})
