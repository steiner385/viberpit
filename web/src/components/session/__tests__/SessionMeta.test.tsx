import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { SessionMeta } from '../SessionMeta'
import type { SubAgent } from '@/session/selectors'
import type { UsageWindow } from '@/api/types'

const baseProps = {
  account: 'work-max',
  model: 'claude-sonnet-4-5',
  mode: 'default',
  contextPct: 78,
  quota: [
    { key: 'session', label: 'session', pct: 42, resetsAt: '2026-06-19T14:30:00.000Z', durationSeconds: 18000 },
    { key: 'weekly', label: 'weekly', pct: 18, resetsAt: null, durationSeconds: 604800 },
  ] as UsageWindow[],
  subAgents: [] as SubAgent[],
  models: [{ id: 'claude-sonnet-4-5', label: 'Sonnet' }],
  accounts: [{ name: 'work-max' }],
  accountPinned: false,
  onSelectModel: () => {},
  onSelectAccount: () => {},
  onSetMode: () => {},
}

describe('SessionMeta', () => {
  it('shows the current account and model', () => {
    render(<SessionMeta {...baseProps} />)
    // Account is shown as the selected value of the account switcher dropdown.
    expect(screen.getByLabelText('Switch account')).toHaveValue('work-max')
    // Model is shown as the selected value of the model switcher dropdown.
    expect(screen.getByLabelText('Select model')).toHaveValue('claude-sonnet-4-5')
  })

  it('binds the picker to the chosen alias and shows the live model as a separate readout', () => {
    render(
      <SessionMeta
        {...baseProps}
        model="opus"
        liveModel="claude-fable-5"
        models={[{ id: 'default', label: 'Default' }, { id: 'opus', label: 'Opus' }]}
      />,
    )
    // The picker never snaps to the SDK-reported id — that is what pinned it.
    expect(screen.getByLabelText('Select model')).toHaveValue('opus')
    expect(screen.getByLabelText('Model in use')).toHaveTextContent('Fable 5')
  })

  it('selects Default for a null model even before the catalog has loaded', () => {
    render(<SessionMeta {...baseProps} model={null} models={[]} />)
    expect(screen.getByLabelText('Select model')).toHaveValue('default')
    expect(screen.queryByLabelText('Model in use')).not.toBeInTheDocument()
  })

  it('flags a switch that waits for the current turn to finish', () => {
    render(<SessionMeta {...baseProps} model="claude-sonnet-4-5" modelPending />)
    expect(screen.getByLabelText('Model switch pending')).toHaveTextContent(/after this turn/i)
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

  it('shows remaining context percent', () => {
    render(<SessionMeta {...baseProps} />)
    expect(screen.getByLabelText(/remaining context/i)).toHaveTextContent('78%')
  })

  it('renders an em-dash for unknown context', () => {
    render(<SessionMeta {...baseProps} model={null} contextPct={null} />)
    // Context still shows em-dash when unknown.
    expect(screen.getByLabelText(/remaining context/i)).toHaveTextContent('—')
    // Model select is still rendered (not absent) when model is null.
    expect(screen.getByLabelText('Select model')).toBeInTheDocument()
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

  it('plan mode toggle shows aria-checked=false when not in plan mode', () => {
    render(<SessionMeta {...baseProps} mode="default" />)
    expect(screen.getByRole('switch', { name: /plan mode/i })).toHaveAttribute('aria-checked', 'false')
  })

  it('plan mode toggle shows aria-checked=true when in plan mode', () => {
    render(<SessionMeta {...baseProps} mode="plan" />)
    expect(screen.getByRole('switch', { name: /plan mode/i })).toHaveAttribute('aria-checked', 'true')
  })

  it('clicking plan toggle calls onSetMode with "plan" when not in plan mode', () => {
    const onSetMode = vi.fn()
    render(<SessionMeta {...baseProps} mode="default" onSetMode={onSetMode} />)
    fireEvent.click(screen.getByRole('switch', { name: /plan mode/i }))
    expect(onSetMode).toHaveBeenCalledWith('plan')
  })

  it('clicking plan toggle calls onSetMode with "default" when in plan mode', () => {
    const onSetMode = vi.fn()
    render(<SessionMeta {...baseProps} mode="plan" onSetMode={onSetMode} />)
    fireEvent.click(screen.getByRole('switch', { name: /plan mode/i }))
    expect(onSetMode).toHaveBeenCalledWith('default')
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
    render(<SessionMeta {...baseProps} subAgents={subAgents} />)
    const toggle = screen.getByRole('button', { name: /sub-agent/i })
    expect(toggle).toHaveTextContent('2')
    // collapsed: list not shown
    expect(screen.queryByText('find it')).toBeNull()
    fireEvent.click(toggle)
    expect(screen.getByText('Explore')).toBeInTheDocument()
    expect(screen.getByText('Plan')).toBeInTheDocument()
  })

  it('marks the ctx readout danger and shows hint when context nearly full', () => {
    render(<SessionMeta {...baseProps} contextPct={5} />)
    const ctx = screen.getByLabelText('Remaining context')
    expect(ctx.className).toMatch(/red/)
    expect(screen.getByText(/compacts soon/i)).toBeInTheDocument()
  })

  it('stays calm with plenty of context remaining', () => {
    render(<SessionMeta {...baseProps} contextPct={80} />)
    const ctx = screen.getByLabelText('Remaining context')
    expect(ctx.className).not.toMatch(/red|amber/)
  })
})
