import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SessionMeta } from './SessionMeta'
import type { SubAgent } from '@/session/selectors'
import type { UsageWindow } from '@/api/types'
import { STATUS_TONE } from './subAgentStatus'

vi.mock('@/api/hooks', () => ({ useStopSubAgent: () => ({ mutate: vi.fn(), isPending: false }) }))

function Wrapper({ children }: { children: ReactNode }) {
  return (
    <MemoryRouter>
      <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>
    </MemoryRouter>
  )
}

const baseProps = {
  sessionId: 's1',
  account: 'work-max',
  quota: [
    { key: 'session', label: 'session', pct: 42, resetsAt: '2026-06-19T14:30:00.000Z', durationSeconds: 18000 },
    { key: 'weekly', label: 'weekly', pct: 18, resetsAt: null, durationSeconds: 604800 },
  ] as UsageWindow[],
  subAgents: [] as SubAgent[],
  accounts: [{ name: 'work-max' }],
  accountPinned: false,
  onSelectAccount: () => {},
}

describe('SessionMeta', () => {
  it('renders no Background chip when nothing is open', () => {
    render(<SessionMeta {...baseProps} />)
    expect(screen.queryByRole('button', { name: /Background work/ })).toBeNull()
  })
  it('shows a Background chip with the summary and opens the list', () => {
    render(<Wrapper><SessionMeta {...baseProps} backgroundWork={{ rows: [{ toolUseId: 'tu1', kind: 'shell', description: 'pytest -q', status: 'running', live: true }], wakeup: false, wakeupAt: null }} /></Wrapper>)
    const chip = screen.getByRole('button', { name: 'Background work: 1 shell' })
    expect(chip).toHaveTextContent('1 shell')
    fireEvent.click(chip)
    expect(screen.getByRole('list', { name: 'Background work' })).toHaveTextContent('pytest -q')
  })
  it('opening the Background chip closes the sub-agent popover', () => {
    const agent: SubAgent = { toolId: 'a1', type: 'Explore', description: 'find', status: 'running' }
    render(<Wrapper><SessionMeta {...baseProps} subAgents={[agent]} backgroundWork={{ rows: [{ toolUseId: 'tu1', kind: 'monitor', description: 'ci', status: 'running', live: false }], wakeup: false, wakeupAt: null }} /></Wrapper>)
    fireEvent.click(screen.getByRole('button', { name: '1 sub-agents' }))
    expect(screen.getByRole('list', { name: 'Sub-agents' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Background work: 1 monitor' }))
    expect(screen.queryByRole('list', { name: 'Sub-agents' })).toBeNull()
    expect(screen.getByRole('list', { name: 'Background work' })).toBeInTheDocument()
  })
  it('shows the current account', () => {
    render(<SessionMeta {...baseProps} />)
    // Account is shown as the selected value of the account switcher dropdown.
    expect(screen.getByLabelText('Switch account')).toHaveValue('work-max')
  })

  it('carries no model or effort control: those live in the prompt row', () => {
    render(<SessionMeta {...baseProps} engine="codex" />)
    expect(screen.queryByLabelText('Select model')).toBeNull()
    expect(screen.queryByLabelText('Reasoning effort')).toBeNull()
  })

  it('labels the account option with the nickname but keeps the slug as its value', () => {
    render(
      <SessionMeta
        {...baseProps}
        account="work-max"
        accounts={[{ name: 'work-max', nickname: 'tony@kindash.com' }]}
      />,
    )
    const select = screen.getByLabelText('Switch account') as HTMLSelectElement
    // Value (the wire identity) stays the slug…
    expect(select).toHaveValue('work-max')
    // …but the visible option text is the chosen nickname, never the slug.
    const opt = Array.from(select.options).find((o) => o.value === 'work-max')
    expect(opt?.text).toBe('tony@kindash.com')
    expect(screen.queryByText('work-max')).not.toBeInTheDocument()
  })

  it('falls back to the slug as the option label when no nickname is set', () => {
    render(
      <SessionMeta {...baseProps} account="work-max" accounts={[{ name: 'work-max' }]} />,
    )
    const select = screen.getByLabelText('Switch account') as HTMLSelectElement
    const opt = Array.from(select.options).find((o) => o.value === 'work-max')
    expect(opt?.text).toBe('work-max')
  })

  it('shows account quota when present', () => {
    render(<SessionMeta {...baseProps} />)
    const quota = screen.getByLabelText(/account quota/i)
    expect(quota).toHaveTextContent('42%')
    expect(quota).toHaveTextContent('18%')
  })

  it('labels each quota window and shows its reset time in a tooltip', () => {
    render(<SessionMeta {...baseProps} />)
    // The session window has a reset time → tooltip names the window + reset.
    const sess = screen.getByText(/sess 42%/i)
    expect(sess.getAttribute('title')).toMatch(/session limit/i)
    expect(sess.getAttribute('title')).toMatch(/resets/i)
    expect(sess.getAttribute('title')).toMatch(/jun/i)
    // The weekly window has no active reset → tooltip says so.
    const wk = screen.getByText(/wk 18%/i)
    expect(wk.getAttribute('title')).toMatch(/weekly limit/i)
    expect(wk.getAttribute('title')).toMatch(/no active window/i)
  })

  it('omits the quota segment when quota is null', () => {
    render(<SessionMeta {...baseProps} quota={null} />)
    expect(screen.queryByLabelText(/account quota/i)).toBeNull()
  })

  it('renders exactly one readout for a single-window provider', () => {
    // Copilot meters ONE premium-request allowance. The previous two-field shape
    // forced it to be shown twice, under two labels, from one number.
    render(<SessionMeta {...baseProps} quota={[
      { key: 'premium', label: 'premium-req', pct: 20, resetsAt: null },
    ]} />)
    const quota = screen.getByLabelText(/account quota/i)
    expect(quota).toHaveTextContent('prem 20%')
    expect(screen.queryByText(/sess /i)).toBeNull()
    expect(screen.queryByText(/wk /i)).toBeNull()
  })

  it('has no Plan mode control: mode moved to the prompt row picker (D-1)', () => {
    render(<SessionMeta {...baseProps} />)
    expect(screen.queryByRole('switch', { name: /plan mode/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /plan mode/i })).toBeNull()
  })

  it('omits the sub-agents control when there are none', () => {
    render(<SessionMeta {...baseProps} subAgents={[]} />)
    expect(screen.queryByRole('button', { name: /sub-agent/i })).toBeNull()
  })

  it('toggles the sub-agent list open on click', () => {
    const subAgents: SubAgent[] = [
      { toolId: 'a', type: 'Explore', description: 'find it', status: 'running' },
      { toolId: 'b', type: 'Plan', description: 'plan it', status: 'done' },
    ]
    // The opened list's rows are links (useNavigate), so this one test needs a router.
    render(<MemoryRouter><SessionMeta {...baseProps} subAgents={subAgents} /></MemoryRouter>)
    const toggle = screen.getByRole('button', { name: /sub-agent/i })
    expect(toggle).toHaveTextContent('2')
    // collapsed: list not shown
    expect(screen.queryByText('find it')).toBeNull()
    fireEvent.click(toggle)
    expect(screen.getByText('Explore')).toBeInTheDocument()
    expect(screen.getByText('Plan')).toBeInTheDocument()
  })

  it('renders the summary dot in the stopped tone when every sub-agent stopped (#1562)', () => {
    const subAgents: SubAgent[] = [
      { toolId: 'a', type: 'Explore', description: 'find it', status: 'stopped' },
      { toolId: 'b', type: 'Plan', description: 'plan it', status: 'stopped' },
    ]
    render(<MemoryRouter><SessionMeta {...baseProps} subAgents={subAgents} /></MemoryRouter>)
    const toggle = screen.getByRole('button', { name: /sub-agent/i })
    const dot = toggle.querySelector('span[aria-hidden]')
    expect(dot).not.toBeNull()
    for (const cls of STATUS_TONE.stopped.split(' ')) {
      expect(dot).toHaveClass(cls)
    }
  })

  // ── session identity: agent, runner, reasoning effort (Task 5.3) ───────────

  it('names the agent when the session does not run the default engine', () => {
    render(<SessionMeta {...baseProps} engine="codex" />)
    expect(screen.getByLabelText('Agent')).toHaveTextContent('Codex')
  })

  it('shows no agent readout for a Claude session', () => {
    render(<SessionMeta {...baseProps} engine="claude_code" />)
    expect(screen.queryByLabelText('Agent')).toBeNull()
  })

  it('shows the runner host the session is executing on', () => {
    render(<SessionMeta {...baseProps} host="rocky" />)
    expect(screen.getByLabelText('Runner host')).toHaveTextContent('rocky')
  })

  it('omits the runner readout when the deployment has no host registry', () => {
    render(<SessionMeta {...baseProps} host={null} />)
    expect(screen.queryByLabelText('Runner host')).toBeNull()
  })

  it('explains a virtual display fallback, and only then', () => {
    const { rerender } = render(
      <SessionMeta {...baseProps} displayMode="virtual" displayFallbackReason="cookie_missing" />)
    expect(screen.getByTestId('meta-display-fallback')).toHaveAttribute(
      'title', expect.stringContaining('Desktop unavailable'))
    expect(screen.getByTestId('meta-display-fallback-reason')).toHaveTextContent(
      'Desktop unavailable — opened on a virtual display: the desktop session changed or is not responding')
    rerender(<SessionMeta {...baseProps} displayMode="virtual" displayFallbackReason={null} />)
    expect(screen.queryByTestId('meta-display-fallback')).toBeNull()
  })

  it('hides the sub-agent control for an engine without the subagents capability', () => {
    const subAgents: SubAgent[] = [
      { toolId: 'a', type: 'Explore', description: 'find it', status: 'running' },
    ]
    render(<SessionMeta {...baseProps} subAgents={subAgents} subAgentsSupported={false} />)
    expect(screen.queryByRole('button', { name: /sub-agent/i })).toBeNull()
  })
})
