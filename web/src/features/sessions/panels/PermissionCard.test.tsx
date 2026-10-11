import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { PermissionCard } from './PermissionCard'
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
  tool: 'mcp__viberpit__delete_session',
  toolKind: 'app_action',
  input: {},
  alwaysAllowed: false,
}

const sugPerm: PermBlock = {
  ...bashPerm,
  ruleSuggestions: [{ pattern: 'Bash(npm test)', description: 'Run npm test', level: 'ok' }],
}

const ruleProps = () => ({
  hasRepo: true,
  onPreviewRule: vi.fn().mockResolvedValue({
    valid: true, level: 'ok', reasons: [], description: 'd', needsConfirmation: false, coversRequest: true, alsoPending: 0,
  }),
  onGrantRule: vi.fn().mockResolvedValue({}),
})

describe('PermissionCard — Always allow… chooser', () => {
  it('labels the buttons Allow once / Deny / Always allow…', () => {
    render(<PermissionCard block={sugPerm} onDecision={vi.fn()} {...ruleProps()} />)
    expect(screen.getByRole('button', { name: /^Allow Bash/ })).toHaveTextContent(/^Allow once$/)
    expect(screen.getByRole('button', { name: /^Always allow Bash/ })).toHaveTextContent(/^Always allow…$/)
  })

  it('shows it in interactive mode when suggestions exist', () => {
    render(<PermissionCard block={sugPerm} onDecision={vi.fn()} {...ruleProps()} />)
    expect(screen.getByRole('button', { name: /^Always allow Bash/ })).toBeInTheDocument()
  })

  it('opens the dialog instead of deciding, and digit 3 / A open it too', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={sugPerm} onDecision={onDecision} {...ruleProps()} />)
    await user.click(screen.getByRole('button', { name: /^Always allow Bash/ }))
    expect(screen.getByRole('dialog', { name: 'Always allow' })).toBeInTheDocument()
    expect(onDecision).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    await user.keyboard('3')
    expect(screen.getByRole('dialog', { name: 'Always allow' })).toBeInTheDocument()
  })

  it('A opens the chooser when focus is in the card', async () => {
    const user = userEvent.setup()
    render(<PermissionCard block={sugPerm} onDecision={vi.fn()} {...ruleProps()} />)
    await user.keyboard('a')
    expect(screen.getByRole('dialog', { name: 'Always allow' })).toBeInTheDocument()
  })

  it('without rule props, suggestions still present, labels the direct session-wide grant honestly', () => {
    render(<PermissionCard block={sugPerm} onDecision={vi.fn()} />)
    const btn = screen.getByRole('button', { name: /^Always allow Bash/ })
    expect(btn).toHaveTextContent('Always allow (this session)')
    expect(btn).toHaveAttribute('title', 'Grant this for the rest of the session')
  })

  it('without rule props, Always allow… falls back to the direct decision', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={sugPerm} onDecision={onDecision} />)
    await user.click(screen.getByRole('button', { name: /^Always allow Bash/ }))
    expect(onDecision).toHaveBeenCalledWith('perm-1', 'always')
  })

  it('engine-managed card (no suggestions) keeps the legacy session-wide button', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={bashPerm} onDecision={onDecision} {...ruleProps()} />)
    const btn = screen.getByRole('button', { name: /^Always allow Bash/ })
    expect(btn).toHaveTextContent('Always allow (this session)')
    await user.click(btn)
    expect(onDecision).toHaveBeenCalledWith('perm-1', 'always')
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

describe('PermissionCard', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('renders three buttons in normal mode (Yes, No, Yes always)', () => {
    render(<PermissionCard block={bashPerm} onDecision={vi.fn()} />)
    // Use exact aria-label patterns; "Allow" button must not match "Always allow"
    expect(screen.getByRole('button', { name: /^Allow Bash/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Deny Bash/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Always allow Bash/i })).toBeInTheDocument()
  })

  it('shows "Yes, always" button in auto mode', () => {
    render(<PermissionCard block={bashPerm} onDecision={vi.fn()} />)
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

  it('ArrowDown then Enter denies', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={bashPerm} onDecision={onDecision} />)
    await user.keyboard('{ArrowDown}{Enter}')
    expect(onDecision).toHaveBeenCalledWith('perm-1', 'deny')
  })

  it('hides "Yes, always" for a destructive app_action even in auto mode', () => {
    render(<PermissionCard block={destructiveAppActionPerm} onDecision={vi.fn()} />)
    expect(screen.getByRole('button', { name: /^Allow /i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Deny /i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Always allow/i })).toBeNull()
  })

  it('digit 3 is ignored for a destructive app_action (no always button)', async () => {
    const user = userEvent.setup()
    const onDecision = vi.fn()
    render(<PermissionCard block={destructiveAppActionPerm} onDecision={onDecision} />)
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

  it('lists batch items on the card', () => {
    const batchPerm: PermBlock = {
      ...bashPerm,
      id: 'perm-4',
      permId: 'perm-4',
      tool: 'mcp__viberpit__archive_sessions',
      toolKind: 'app_action',
      input: {},
      items: ['Fix auth', 'CSV importer'],
    }
    render(<PermissionCard block={batchPerm} onDecision={vi.fn()} />)
    expect(screen.getByText('Fix auth')).toBeInTheDocument()
    expect(screen.getByText('CSV importer')).toBeInTheDocument()
  })

  it('renders no item list when items is absent', () => {
    render(<PermissionCard block={bashPerm} onDecision={vi.fn()} />)
    expect(screen.queryByRole('list')).toBeNull()
  })
})

// 2026-09-29 Sessions critique, P0: the command the operator is approving was a
// one-line `text-xs truncate` of a 60-char summary, and "Yes, always" — the
// broadest grant — wore the heaviest button. The card is the highest-stakes tap
// on the surface; it must show the whole request and weight the safe answer.
describe('PermissionCard — readable request, honest weighting', () => {
  const LONG_CMD = 'cd /srv/repo && git fetch origin && git rebase origin/master && npm ci && npm run build && npm run test -- --reporter=dot && git push --force-with-lease origin feat/x'

  it('shows the FULL command, not the 60-character summary', () => {
    render(<PermissionCard block={{ ...bashPerm, input: { command: LONG_CMD } }} onDecision={vi.fn()} />)
    expect(screen.getByTestId('permission-detail')).toHaveTextContent(LONG_CMD)
  })

  it('keeps the request block scrollable rather than clipped to one line', () => {
    render(<PermissionCard block={{ ...bashPerm, input: { command: LONG_CMD } }} onDecision={vi.fn()} />)
    const detail = screen.getByTestId('permission-detail')
    expect(detail).not.toHaveClass('truncate')
    expect(detail).toHaveClass('overflow-y-auto')
    // Keyboard users can scroll it.
    expect(detail).toHaveAttribute('tabindex', '0')
  })

  it('names the actions on the buttons (Allow / Deny / Always allow), never Yes / No', () => {
    render(<PermissionCard block={bashPerm} onDecision={vi.fn()} />)
    expect(screen.getByRole('button', { name: /^Allow Bash/ })).toHaveTextContent(/^Allow once$/)
    expect(screen.getByRole('button', { name: /^Deny Bash/ })).toHaveTextContent(/^Deny$/)
    expect(screen.getByRole('button', { name: /^Always allow Bash/ })).toHaveTextContent(/^Always allow \(this session\)$/)
    expect(screen.queryByText(/^Yes/)).toBeNull()
  })

  it('weights Allow as the primary action and Always allow as a quiet tertiary one', () => {
    render(<PermissionCard block={bashPerm} onDecision={vi.fn()} />)
    const allow = screen.getByRole('button', { name: /^Allow Bash/ })
    const always = screen.getByRole('button', { name: /^Always allow Bash/ })
    expect(document.activeElement).toBe(allow)
    // The filled tokens belong to Allow (success) and to the plan card's accent
    // primary — never to the standing grant.
    expect(always).not.toHaveClass('bg-accent')
    expect(always).not.toHaveClass('bg-success-strong')
    expect(always.className).toMatch(/text-ink-muted/)
  })

  it('shows every changed path for a multi-file edit', () => {
    render(
      <PermissionCard
        block={{ ...bashPerm, tool: 'Edit', toolKind: 'edit', input: { changes: [{ path: 'web/src/a.tsx' }, { path: 'web/src/b.tsx' }, { path: 'server/c.py' }] } }}
        onDecision={vi.fn()}
      />,
    )
    const detail = screen.getByTestId('permission-detail')
    expect(detail).toHaveTextContent('web/src/a.tsx')
    expect(detail).toHaveTextContent('web/src/b.tsx')
    expect(detail).toHaveTextContent('server/c.py')
  })

  it('shows the raw input for a tool shape it does not summarise, so nothing is approved blind', () => {
    render(
      <PermissionCard
        block={{ ...bashPerm, tool: 'mcp__viberpit__archive_sessions', toolKind: 'app_action', input: { sessionIds: ['abc-123'], reason: 'stale' } }}
        onDecision={vi.fn()}
      />,
    )
    expect(screen.getByTestId('permission-detail')).toHaveTextContent('abc-123')
  })

  it('renders no detail block when there is nothing beyond the tool name', () => {
    render(<PermissionCard block={{ ...bashPerm, tool: 'WebSearch', toolKind: 'web', input: {} }} onDecision={vi.fn()} />)
    expect(screen.queryByTestId('permission-detail')).toBeNull()
  })

  it('lets long batch items wrap instead of clipping them', () => {
    render(
      <PermissionCard
        block={{ ...bashPerm, tool: 'mcp__viberpit__archive_sessions', toolKind: 'app_action', input: {}, items: ['A very long session title that would otherwise be clipped to one line with an ellipsis on a phone'] }}
        onDecision={vi.fn()}
      />,
    )
    expect(screen.getByRole('listitem')).not.toHaveClass('truncate')
  })
})
