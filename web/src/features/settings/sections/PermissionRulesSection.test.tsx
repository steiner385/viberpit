import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import * as hooks from '@/api/hooks'
import { ApiRequestError } from '@/api/client'
import type { RuleWire } from '@/api/types'
import { queryResult, mutationResult } from '@/test/reactQuery'
import { PermissionRulesSection } from './PermissionRulesSection'

vi.mock('@/api/hooks', () => ({
  usePermissionRules: vi.fn(),
  useUpdatePermissionRule: vi.fn(),
  useRevokePermissionRule: vi.fn(),
}))

const ago = (ms: number) => new Date(Date.now() - ms).toISOString()

function rule(over: Partial<RuleWire>): RuleWire {
  return {
    id: 'r1', profileId: 'p', pattern: 'Bash(git status)', description: 'Run git status',
    scope: 'global', scopeKey: null, position: 0, createdBy: 'tony', createdVia: 'card',
    createdAt: ago(2 * 3600_000), hitCount: 3, lastHitAt: ago(60_000), ...over,
  }
}

const RULES: RuleWire[] = [
  rule({ id: 'g1', pattern: 'Read', description: 'Read any file' }),
  rule({ id: 'r-a', scope: 'repo', scopeKey: 'acme/web', pattern: 'Bash(npm test)', description: 'Run npm test' }),
  rule({ id: 's1', scope: 'session', scopeKey: 'abcdef123456', pattern: 'Edit', description: 'Edit files', hitCount: 0, lastHitAt: null }),
]

describe('PermissionRulesSection', () => {
  const update = vi.fn()
  const revoke = vi.fn()
  beforeEach(() => {
    update.mockReset().mockResolvedValue({})
    revoke.mockReset().mockResolvedValue(undefined)
    vi.mocked(hooks.usePermissionRules).mockReturnValue(queryResult(RULES))
    vi.mocked(hooks.useUpdatePermissionRule).mockReturnValue(
      mutationResult({ mutateAsync: update } as never) as never)
    vi.mocked(hooks.useRevokePermissionRule).mockReturnValue(
      mutationResult({ mutateAsync: revoke } as never) as never)
  })
  const mount = () => render(<MemoryRouter><PermissionRulesSection /></MemoryRouter>)

  it('groups by scope: all sessions, repos by key, sessions linked', () => {
    mount()
    expect(screen.getByText('All sessions')).toBeInTheDocument()
    expect(screen.getByText('acme/web')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: 'abcdef12' })
    expect(link).toHaveAttribute('href', '/sessions/abcdef123456')
    expect(screen.getByText('Read')).toBeInTheDocument()
    expect(screen.getByText(/Only a person can create one/)).toBeInTheDocument()
  })

  it('shows who made each rule and its hit stats', () => {
    mount()
    expect(screen.getAllByText(/by tony · 2h ago/).length).toBe(3)
    expect(screen.getAllByText('3 hits · last 1m ago').length).toBe(2)
    expect(screen.getByText('0 hits · last never')).toBeInTheDocument()
  })

  it('edits a pattern inline and saves it', async () => {
    const user = userEvent.setup()
    mount()
    await user.click(within(screen.getByTestId('rule-g1')).getByRole('button', { name: 'Edit' }))
    const input = screen.getByLabelText('Pattern')
    await user.clear(input)
    await user.type(input, 'Read(src/**)')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(update).toHaveBeenCalledWith({ id: 'g1', pattern: 'Read(src/**)' }))
    await waitFor(() => expect(screen.queryByLabelText('Pattern')).toBeNull())
  })

  it('409 needs-confirmation shows reasons, then retries with confirm only on click', async () => {
    const user = userEvent.setup()
    update.mockRejectedValueOnce(new ApiRequestError(409, {
      detail: { code: 'RULE_NEEDS_CONFIRMATION', level: 'high', reasons: ['Matches every shell command'] },
    }))
    mount()
    await user.click(within(screen.getByTestId('rule-g1')).getByRole('button', { name: 'Edit' }))
    await user.clear(screen.getByLabelText('Pattern'))
    await user.type(screen.getByLabelText('Pattern'), 'Bash(*)')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Matches every shell command')).toBeInTheDocument()
    expect(update).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: 'Save anyway' }))
    await waitFor(() => expect(update).toHaveBeenLastCalledWith({ id: 'g1', pattern: 'Bash(*)', confirm: true }))
  })

  it('shows a server refusal inline', async () => {
    const user = userEvent.setup()
    update.mockRejectedValueOnce(new ApiRequestError(409, {
      detail: { code: 'DUPLICATE_RULE', message: 'That scope already has this rule.' },
    }))
    mount()
    await user.click(within(screen.getByTestId('rule-g1')).getByRole('button', { name: 'Edit' }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('That scope already has this rule.')
  })

  it('revoke goes through the confirm, then DELETEs', async () => {
    const user = userEvent.setup()
    mount()
    await user.click(within(screen.getByTestId('rule-g1')).getByRole('button', { name: 'Revoke' }))
    expect(revoke).not.toHaveBeenCalled()
    expect(screen.getByText(/Matching requests will ask again/)).toBeInTheDocument()
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(revoke).toHaveBeenCalledWith('g1'))
  })

  it('cancelling the revoke confirm does nothing', async () => {
    const user = userEvent.setup()
    mount()
    await user.click(within(screen.getByTestId('rule-g1')).getByRole('button', { name: 'Revoke' }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(revoke).not.toHaveBeenCalled()
  })

  it('a 409 for a stale pattern never arms Save anyway for newer text', async () => {
    const user = userEvent.setup()
    let reject!: (e: unknown) => void
    update.mockReturnValueOnce(new Promise((_, rej) => { reject = rej }))
    mount()
    await user.click(within(screen.getByTestId('rule-g1')).getByRole('button', { name: 'Edit' }))
    await user.clear(screen.getByLabelText('Pattern'))
    await user.type(screen.getByLabelText('Pattern'), 'Bash(*)')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await user.type(screen.getByLabelText('Pattern'), 'x')
    reject(new ApiRequestError(409, {
      detail: { code: 'RULE_NEEDS_CONFIRMATION', level: 'high', reasons: ['Too broad'] },
    }))
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByText('Too broad')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save anyway' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()
  })

  it('disables the pattern input while a save is pending', async () => {
    const user = userEvent.setup()
    vi.mocked(hooks.useUpdatePermissionRule).mockReturnValue(
      mutationResult({ mutateAsync: update, isPending: true } as never) as never)
    mount()
    await user.click(within(screen.getByTestId('rule-g1')).getByRole('button', { name: 'Edit' }))
    expect(screen.getByLabelText('Pattern')).toBeDisabled()
  })

  it('surfaces a revoke failure inline on the row', async () => {
    const user = userEvent.setup()
    revoke.mockRejectedValueOnce(new ApiRequestError(404, { detail: { code: 'RULE_NOT_FOUND', message: 'No such rule.' } }))
    mount()
    await user.click(within(screen.getByTestId('rule-g1')).getByRole('button', { name: 'Revoke' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Revoke' }))
    expect(await within(screen.getByTestId('rule-g1')).findByRole('alert')).toHaveTextContent('No such rule.')
  })

  it('shows an error state, not the empty state, when the query fails', () => {
    vi.mocked(hooks.usePermissionRules).mockReturnValue(
      queryResult(undefined as unknown as RuleWire[], { isError: true, error: new Error('boom') }))
    mount()
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load')
    expect(screen.queryByText(/No standing rules yet/)).toBeNull()
  })

  it('renders the empty state', () => {
    vi.mocked(hooks.usePermissionRules).mockReturnValue(queryResult([] as RuleWire[]))
    mount()
    expect(screen.getByText(/No standing rules yet/)).toBeInTheDocument()
  })
})
