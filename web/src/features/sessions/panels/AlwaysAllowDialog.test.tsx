import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AlwaysAllowDialog } from './AlwaysAllowDialog'
import { ApiRequestError } from '@/api/client'
import type { RulePreview } from '@/api/types'
import type { Block } from '@/session/reducer'

type PermBlock = Extract<Block, { kind: 'permission' }>

const block: PermBlock = {
  kind: 'permission', id: 'p1', permId: 'p1', tool: 'Bash', toolKind: 'bash',
  input: { command: 'npm test' }, plan: false,
  ruleSuggestions: [
    { pattern: 'Bash(npm test)', description: 'Run npm test', level: 'ok' },
    { pattern: 'Bash(npm *)', description: 'Any npm command', level: 'warn' },
  ],
}

const okPreview: RulePreview = {
  valid: true, level: 'ok', reasons: [], description: 'Runs npm test',
  needsConfirmation: false, coversRequest: true, alsoPending: 0,
}

function setup(over: Partial<React.ComponentProps<typeof AlwaysAllowDialog>> = {}) {
  const onPreview = vi.fn().mockResolvedValue(okPreview)
  const onGrant = vi.fn().mockResolvedValue(undefined)
  const onClose = vi.fn()
  render(
    <AlwaysAllowDialog open block={block} hasRepo onPreview={onPreview} onGrant={onGrant} onClose={onClose} {...over} />,
  )
  return { onPreview, onGrant, onClose }
}

const patternInput = () => screen.getByLabelText('Pattern') as HTMLInputElement

describe('AlwaysAllowDialog', () => {
  it('pre-fills the pattern from the first suggestion and previews it', async () => {
    const { onPreview } = setup()
    expect(patternInput().value).toBe('Bash(npm test)')
    await waitFor(() => expect(onPreview).toHaveBeenCalledWith('Bash(npm test)', 'session'))
    expect(await screen.findByText('Runs npm test')).toBeInTheDocument()
  })

  it('picking a suggestion overwrites the pattern', async () => {
    const user = userEvent.setup()
    setup()
    await user.click(screen.getByLabelText(/Bash\(npm \*\)/))
    expect(patternInput().value).toBe('Bash(npm *)')
  })

  it('editing the pattern triggers a new preview', async () => {
    const user = userEvent.setup()
    const { onPreview } = setup()
    await user.clear(patternInput())
    await user.type(patternInput(), 'Bash(ls)')
    await waitFor(() => expect(onPreview).toHaveBeenLastCalledWith('Bash(ls)', 'session'))
  })

  it('disables the repo scope for scratch sessions', () => {
    setup({ hasRepo: false })
    expect(screen.getByLabelText('This repo')).toBeDisabled()
    expect(screen.getByText('Scratch session — no repo')).toBeInTheDocument()
    expect(screen.getByLabelText('This session')).toBeChecked()
  })

  it('shows not-covered and also-pending preview lines and blocks grant when not covered', async () => {
    setup({ onPreview: vi.fn().mockResolvedValue({ ...okPreview, coversRequest: false, alsoPending: 2 }) })
    expect(await screen.findByText("Doesn't cover this request")).toBeInTheDocument()
    expect(screen.getByText('Also resolves 2 other waiting requests')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Grant' })).toBeDisabled()
  })

  it('blocks grant on a refusal and shows its reasons', async () => {
    setup({ onPreview: vi.fn().mockResolvedValue({ ...okPreview, valid: false, level: 'refuse', reasons: ['Too broad'] }) })
    expect(await screen.findByText('Too broad')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Grant' })).toBeDisabled()
  })

  it('grants with confirm=false and closes', async () => {
    const user = userEvent.setup()
    const { onGrant, onClose } = setup()
    await user.click(screen.getByRole('button', { name: 'Grant' }))
    expect(onGrant).toHaveBeenCalledWith('Bash(npm test)', 'session', false)
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('Enter in the pattern field grants', async () => {
    const user = userEvent.setup()
    const { onGrant } = setup()
    await user.type(patternInput(), '{Enter}')
    expect(onGrant).toHaveBeenCalledWith('Bash(npm test)', 'session', false)
  })

  it('a 409 shows the confirm step; only the second click sends confirm=true', async () => {
    const user = userEvent.setup()
    const onGrant = vi.fn()
      .mockRejectedValueOnce(new ApiRequestError(409, { detail: { code: 'RULE_NEEDS_CONFIRMATION', message: 'm', level: 'warn', reasons: ['Matches many commands'] } }))
      .mockResolvedValueOnce(undefined)
    const { onClose } = setup({ onGrant })
    await user.click(screen.getByRole('button', { name: 'Grant' }))
    expect(await screen.findByText('Matches many commands')).toBeInTheDocument()
    expect(onGrant).toHaveBeenCalledTimes(1)
    expect(onClose).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(onGrant).toHaveBeenLastCalledWith('Bash(npm test)', 'session', true)
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('global scope confirm button reads "Confirm — allow everywhere"', async () => {
    const user = userEvent.setup()
    setup({ onPreview: vi.fn().mockResolvedValue({ ...okPreview, level: 'warn', needsConfirmation: true, reasons: ['x'] }) })
    await user.click(screen.getByLabelText('All sessions'))
    expect(await screen.findByRole('button', { name: 'Confirm — allow everywhere' })).toBeInTheDocument()
  })

  it('PERMISSION_NOT_PENDING shows the stale message with a Close button', async () => {
    const user = userEvent.setup()
    const onGrant = vi.fn().mockRejectedValue(new ApiRequestError(404, { detail: { code: 'PERMISSION_NOT_PENDING', message: 'gone' } }))
    const { onClose } = setup({ onGrant })
    await user.click(screen.getByRole('button', { name: 'Grant' }))
    expect(await screen.findByText('This request is no longer pending')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Close' }))
    expect(onClose).toHaveBeenCalled()
  })

  it('shows the message of any other error', async () => {
    const user = userEvent.setup()
    const onGrant = vi.fn().mockRejectedValue(new ApiRequestError(400, { detail: { code: 'NO_REPO', message: 'Session has no repo' } }))
    setup({ onGrant })
    await user.click(screen.getByRole('button', { name: 'Grant' }))
    expect(await screen.findByText('Session has no repo')).toBeInTheDocument()
  })
})
