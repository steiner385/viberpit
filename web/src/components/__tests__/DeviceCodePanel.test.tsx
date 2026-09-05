/**
 * DeviceCodePanel — the shared half of every device-code enrolment.
 *
 * Three call sites drive it (Copilot add, OpenAI add, OpenAI reauth), so these
 * assertions are the contract that keeps them from drifting apart.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { DeviceCodePanel } from '../DeviceCodePanel'
import type { DeviceCodeState } from '@/api/types'

function state(over: Partial<DeviceCodeState> = {}): DeviceCodeState {
  return { state: 'pending', userCode: null, verificationUri: null, ...over }
}

beforeEach(() => window.localStorage.clear())

describe('DeviceCodePanel', () => {
  it('renders nothing before a flow starts', () => {
    const { container } = render(<DeviceCodePanel started={false} providerLabel="ChatGPT" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('stays silent about `pending` until the user actually started the flow', () => {
    const { container } = render(
      <DeviceCodePanel state={state()} started={false} providerLabel="ChatGPT" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('narrates the pending state once started', () => {
    render(<DeviceCodePanel state={state()} started providerLabel="ChatGPT" />)
    expect(screen.getByText(/starting device authorization/i)).toBeInTheDocument()
  })

  it('narrates the start request before the first status response arrives', () => {
    render(<DeviceCodePanel started providerLabel="ChatGPT" />)
    expect(screen.getByText(/starting device authorization/i)).toBeInTheDocument()
  })

  it('shows the code, names the provider, and opens the link safely', () => {
    render(<DeviceCodePanel
      state={state({
        state: 'awaiting_authorization',
        userCode: 'ABCD-1234',
        verificationUri: 'https://github.com/login/device',
      })}
      started
      providerLabel="GitHub"
    />)
    expect(screen.getByText('ABCD-1234')).toBeInTheDocument()
    expect(screen.getByText(/authorize with GitHub/)).toBeInTheDocument()
    const link = screen.getByRole('link')
    expect(link).toHaveAttribute('href', 'https://github.com/login/device')
    expect(link).toHaveAttribute('rel', expect.stringContaining('noreferrer'))
    // Typed on a phone as often as clicked.
    expect(link.className).toContain('min-h-[44px]')
  })

  it('reports a failure as an alert, preferring the server-scrubbed reason', () => {
    render(<DeviceCodePanel
      state={state({ state: 'failed', error: 'account already enrolled' })}
      started providerLabel="ChatGPT" />)
    expect(screen.getByRole('alert')).toHaveTextContent('account already enrolled')
  })

  it('falls back to a fixed line when the server gave no reason', () => {
    render(<DeviceCodePanel state={state({ state: 'failed' })} started providerLabel="ChatGPT" />)
    expect(screen.getByRole('alert')).toHaveTextContent(/authorization failed/i)
  })

  it('shows nothing once authorized — the code must not linger on screen', () => {
    const { container } = render(
      <DeviceCodePanel state={state({ state: 'authorized', userCode: 'ABCD-1234' })}
        started providerLabel="ChatGPT" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('persists nothing: the code never reaches storage', () => {
    // Security invariant. `userCode` and `verificationUri` are the only login
    // details the authority may emit; they must not reach query-cache
    // persistence, analytics, or an error/telemetry payload.
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    render(<DeviceCodePanel
      state={state({
        state: 'awaiting_authorization',
        userCode: 'ABCD-1234',
        verificationUri: 'https://github.com/login/device',
      })}
      started providerLabel="GitHub" />)
    expect(setItem).not.toHaveBeenCalled()
    expect(JSON.stringify(window.localStorage)).not.toContain('ABCD-1234')
    setItem.mockRestore()
  })
})
