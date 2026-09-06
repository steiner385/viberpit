import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { it, expect, vi, beforeEach } from 'vitest'
import { DispatchBar, type DispatchBarProps } from '../DispatchBar'
import { ENGINE_PREFERENCE_KEY } from '@/lib/enginePreference'
import type { AgentCapabilities, EngineOffer } from '@/api/types'

/**
 * The props `DispatchBar` requires but this suite does not exercise. They were
 * simply omitted before, so every render here was missing `onNavigate`,
 * `onEnterList`, `onJump`, `onShowHelp`, `onEscape` and `isPending` — the
 * component would have thrown on any key that reaches one of them.
 */
const required: Omit<DispatchBarProps, 'repos' | 'reposLoading' | 'reposError' | 'onDispatch'> = {
  onNavigate: vi.fn(),
  onOpenHighlighted: vi.fn(),
  onEnterList: vi.fn(),
  onJump: vi.fn(),
  onShowHelp: vi.fn(),
  onEscape: vi.fn(),
  isPending: false,
}

beforeEach(() => {
  URL.createObjectURL = vi.fn(() => 'blob:p')
  URL.revokeObjectURL = vi.fn()
  window.localStorage.clear()
  vi.stubGlobal('fetch', vi.fn(async () =>
    ({ ok: true, json: async () => ({ id: 'att-1', mediaType: 'image/png', filename: 'x.png', byteSize: 10, url: '/api/attachments/att-1' }) })))
})

function caps(over: Partial<AgentCapabilities> = {}): AgentCapabilities {
  return {
    models: true, reasoningEffort: false, steer: true, interrupt: true,
    approvals: true, questions: true, fork: true, adopt: true,
    selfModelSwitch: true, subagents: true, attachments: true, accountRotation: true,
    ...over,
  }
}

function offer(over: Partial<EngineOffer> = {}): EngineOffer {
  return {
    engine: 'claude_code', available: true, hosts: ['edge'],
    unavailableReasons: {}, blockedReason: null, capabilities: caps(), ...over,
  }
}

const BOTH_ENGINES: EngineOffer[] = [offer(), offer({ engine: 'codex' })]

it('rejects a PDF for a codex-selected session (#1276)', async () => {
  window.localStorage.setItem(ENGINE_PREFERENCE_KEY, 'codex')
  const onDispatch = vi.fn().mockResolvedValue(undefined)
  const fetchSpy = vi.fn()
  vi.stubGlobal('fetch', fetchSpy)
  render(
    <DispatchBar
      {...required}
      repos={[]} reposLoading={false} reposError={false}
      onDispatch={onDispatch} engines={BOTH_ENGINES}
    />,
  )
  const ta = screen.getByRole('textbox')
  fireEvent.paste(ta, { clipboardData: {
    files: [new File([new Uint8Array(10)], 'spec.pdf', { type: 'application/pdf' })] } })
  await waitFor(() => expect(screen.getByTestId('attachment-chips')).toHaveTextContent(/not supported by codex/i))
  expect(fetchSpy).not.toHaveBeenCalled()
})

it('dispatch includes pasted image attachment ids', async () => {
  const onDispatch = vi.fn().mockResolvedValue(undefined)
  render(<DispatchBar {...required} repos={[]} reposLoading={false} reposError={false} onDispatch={onDispatch} />)
  const ta = screen.getByRole('textbox')
  fireEvent.paste(ta, { clipboardData: {
    files: [new File([new Uint8Array(10)], 'x.png', { type: 'image/png' })] } })
  await waitFor(() => screen.getByTestId('attachment-chips'))
  fireEvent.change(ta, { target: { value: 'start with this' } })
  fireEvent.keyDown(ta, { key: 'Enter' })
  await waitFor(() => expect(onDispatch).toHaveBeenCalledWith(expect.objectContaining({
    initialPrompt: 'start with this',
    attachmentIds: ['att-1'] })))
})

it('dispatch with image-only (empty prompt) calls onDispatch not onOpenHighlighted', async () => {
  const onDispatch = vi.fn().mockResolvedValue(undefined)
  const onOpenHighlighted = vi.fn()
  render(
    <DispatchBar
      {...required}
      repos={[]}
      reposLoading={false}
      reposError={false}
      onDispatch={onDispatch}
      onOpenHighlighted={onOpenHighlighted}
    />,
  )
  const ta = screen.getByRole('textbox')
  fireEvent.paste(ta, { clipboardData: {
    files: [new File([new Uint8Array(10)], 'x.png', { type: 'image/png' })] } })
  await waitFor(() => screen.getByTestId('attachment-chips'))
  fireEvent.keyDown(ta, { key: 'Enter' })
  await waitFor(() => expect(onDispatch).toHaveBeenCalledWith(expect.objectContaining({
    initialPrompt: '',
    attachmentIds: ['att-1'] })))
  expect(onOpenHighlighted).not.toHaveBeenCalled()
})

it('file input uses image/* and is not camera-only', () => {
  const { container } = render(<DispatchBar {...required} repos={[]} reposLoading={false} reposError={false} onDispatch={vi.fn().mockResolvedValue(undefined)} />)
  const input = container.querySelector('input[type=file]') as HTMLInputElement
  expect(input.getAttribute('accept')).toBe('image/*')
  expect(input.hasAttribute('capture')).toBe(false)
  expect(input.multiple).toBe(true)
})
