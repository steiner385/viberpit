import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PermissionCard } from '../PermissionCard'
import type { Block } from '@/session/reducer'

type PermBlock = Extract<Block, { kind: 'permission' }>

const bashPerm: PermBlock = {
  kind: 'permission',
  id: 'perm-1',
  permId: 'perm-1',
  tool: 'Bash',
  toolKind: 'bash',
  input: { command: 'npm test' },
  plan: false,
}

const planPerm: PermBlock = {
  ...bashPerm,
  id: 'perm-2',
  permId: 'perm-2',
  plan: true,
}

const destructiveAppActionPerm: PermBlock = {
  ...bashPerm,
  id: 'perm-3',
  permId: 'perm-3',
  tool: 'mcp__ccswitch__delete_session',
  toolKind: 'app_action',
  input: {},
  alwaysAllowed: false,
}

describe('PermissionCard', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('renders three buttons in normal mode (Yes, No, Yes always)', () => {
    render(<PermissionCard block={bashPerm} onDecision={vi.fn()} />)
    // Use exact aria-label patterns; "Allow" button must not match "Always allow"
    expect(screen.getByRole('button', { name: /^Allow Bash/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Deny Bash/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Always allow Bash/i })).toBeInTheDocument()
  })

  it('hides "Yes, always" button in interactive mode', () => {
    render(<PermissionCard block={bashPerm} mode="interactive" onDecision={vi.fn()} />)
    expect(screen.getByRole('button', { name: /^Allow Bash/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Deny Bash/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Always allow Bash/i })).toBeNull()
  })

  it('shows "Yes, always" button in auto mode', () => {
    render(<PermissionCard block={bashPerm} mode="auto" onDecision={vi.fn()} />)
    expect(screen.getByRole('button', { name: /^Always allow Bash/i })).toBeInTheDocument()
  })

  it('shows "Yes, always" button when mode is undefined (default)', () => {
    render(<PermissionCard block={bashPerm} onDecision={vi.fn()} />)
    expect(screen.getByRole('button', { name: /^Always allow Bash/i })).toBeInTheDocument()
  })

  it('renders plan variant buttons when block.plan is true', () => {
    render(<PermissionCard block={planPerm} onDecision={vi.fn()} />)
    expect(screen.getByRole('button', { name: /approve.*run/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /keep planning/i })).toBeInTheDocument()
  })

  it('calls onDecision with allow on Yes click', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={bashPerm} onDecision={onDecision} />)
    await user.click(screen.getByRole('button', { name: /^Allow Bash/i }))
    expect(onDecision).toHaveBeenCalledWith('perm-1', 'allow')
  })

  it('calls onDecision with deny on No click', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={bashPerm} onDecision={onDecision} />)
    await user.click(screen.getByRole('button', { name: /^Deny Bash/i }))
    expect(onDecision).toHaveBeenCalledWith('perm-1', 'deny')
  })

  it('calls onDecision with always on Yes always click', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={bashPerm} onDecision={onDecision} />)
    await user.click(screen.getByRole('button', { name: /^Always allow Bash/i }))
    expect(onDecision).toHaveBeenCalledWith('perm-1', 'always')
  })

  it('moves focus to primary button on mount', () => {
    render(<PermissionCard block={bashPerm} onDecision={vi.fn()} />)
    const primary = screen.getByRole('button', { name: /^Allow Bash/i })
    expect(document.activeElement).toBe(primary)
  })

  it('digit 1 allows, 2 denies, 3 always (normal mode)', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={bashPerm} onDecision={onDecision} />)
    await user.keyboard('1')
    expect(onDecision).toHaveBeenCalledWith('perm-1', 'allow')
    await user.keyboard('2')
    expect(onDecision).toHaveBeenCalledWith('perm-1', 'deny')
    await user.keyboard('3')
    expect(onDecision).toHaveBeenCalledWith('perm-1', 'always')
  })

  it('digit 3 is ignored in interactive mode (no always button)', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={bashPerm} mode="interactive" onDecision={onDecision} />)
    await user.keyboard('3')
    expect(onDecision).not.toHaveBeenCalled()
  })

  it('ArrowDown then Enter denies', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={bashPerm} onDecision={onDecision} />)
    await user.keyboard('{ArrowDown}{Enter}')
    expect(onDecision).toHaveBeenCalledWith('perm-1', 'deny')
  })

  it('hides "Yes, always" for a destructive app_action even in auto mode', () => {
    render(<PermissionCard block={destructiveAppActionPerm} mode="auto" onDecision={vi.fn()} />)
    expect(screen.getByRole('button', { name: /^Allow /i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Deny /i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Always allow/i })).toBeNull()
  })

  it('digit 3 is ignored for a destructive app_action (no always button)', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={destructiveAppActionPerm} mode="auto" onDecision={onDecision} />)
    await user.keyboard('3')
    expect(onDecision).not.toHaveBeenCalled()
  })

  it('plan variant: digit 1 allows, 2 denies', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={planPerm} onDecision={onDecision} />)
    await user.keyboard('1')
    expect(onDecision).toHaveBeenCalledWith(planPerm.permId, 'allow')
    await user.keyboard('2')
    expect(onDecision).toHaveBeenCalledWith(planPerm.permId, 'deny')
  })
})
