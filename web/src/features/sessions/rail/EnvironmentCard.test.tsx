import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { PullRequestInfo } from '@/api/types'
import { EnvironmentCard } from './EnvironmentCard'

const pr = (number: number): PullRequestInfo => ({
  number, url: `https://github.com/o/r/pull/${number}`, state: 'open', checks: 'passing', title: `PR ${number}`, deployed: false,
})

const base = {
  engine: null,
  account: 'work',
  accounts: [{ name: 'work' }, { name: 'home', nickname: 'tony@home' }],
  accountPinned: false,
  onSelectAccount: () => {},
  onUnpin: () => {},
  host: null as string | null,
  displayMode: null as 'native' | 'virtual' | null,
  displayFallbackReason: null as string | null,
  branch: null as string | null,
  cwd: null as string | null,
  prs: [] as PullRequestInfo[],
}

describe('EnvironmentCard', () => {
  it('is a labelled "Environment" section', () => {
    render(<EnvironmentCard {...base} />)
    expect(screen.getByRole('region', { name: 'Environment' })).toBeInTheDocument()
  })

  it('the account select calls onSelectAccount', () => {
    const onSelect = vi.fn()
    render(<EnvironmentCard {...base} onSelectAccount={onSelect} />)
    const select = screen.getByLabelText('Switch account')
    expect(select).toHaveValue('work')
    fireEvent.change(select, { target: { value: 'home' } })
    expect(onSelect).toHaveBeenCalledWith('home')
  })

  it('labels an account option with its nickname but keeps the slug as its value', () => {
    render(<EnvironmentCard {...base} account="home" />)
    const select = screen.getByLabelText('Switch account') as HTMLSelectElement
    expect(select).toHaveValue('home')
    expect(Array.from(select.options).find((o) => o.value === 'home')?.text).toBe('tony@home')
    // No nickname: the slug is the label.
    expect(Array.from(select.options).find((o) => o.value === 'work')?.text).toBe('work')
  })

  it('shows Unpin only when pinned, and it calls onUnpin', () => {
    const onUnpin = vi.fn()
    const { rerender } = render(<EnvironmentCard {...base} onUnpin={onUnpin} />)
    expect(screen.queryByRole('button', { name: 'Unpin account' })).toBeNull()
    rerender(<EnvironmentCard {...base} accountPinned onUnpin={onUnpin} />)
    fireEvent.click(screen.getByRole('button', { name: 'Unpin account' }))
    expect(onUnpin).toHaveBeenCalledTimes(1)
  })

  it('gives the account controls a 44px floor on coarse pointers, width too for the icon-only Unpin', () => {
    render(<EnvironmentCard {...base} accountPinned />)
    const unpin = screen.getByRole('button', { name: 'Unpin account' })
    expect(unpin).toHaveClass('min-h-11', '[@media(hover:hover)]:min-h-0', 'min-w-11', '[@media(hover:hover)]:min-w-0')
    expect(screen.getByLabelText('Switch account')).toHaveClass('min-h-11', '[@media(hover:hover)]:min-h-0')
  })

  it('shows the engine only when it is not the default', () => {
    const { rerender } = render(<EnvironmentCard {...base} />)
    expect(screen.queryByText('Agent')).toBeNull()
    rerender(<EnvironmentCard {...base} engine="codex" />)
    expect(screen.getByText('Agent')).toBeInTheDocument()
  })

  it('shows the host', () => {
    render(<EnvironmentCard {...base} host="rocky" />)
    expect(screen.getByText('rocky')).toBeInTheDocument()
  })

  it('shows a desktop display', () => {
    render(<EnvironmentCard {...base} displayMode="native" />)
    expect(screen.getByText('desktop')).toBeInTheDocument()
  })

  it('shows a virtual display with its reason', () => {
    render(<EnvironmentCard {...base} displayMode="virtual" displayFallbackReason="no_graphical_session" />)
    expect(screen.getByText('virtual')).toBeInTheDocument()
    expect(screen.getByText(/no one is logged in to the desktop/)).toBeInTheDocument()
  })

  it('shows branch and worktree values', () => {
    render(<EnvironmentCard {...base} branch="feat/x" cwd="/home/t/wt/abc" />)
    expect(screen.getByTestId('env-branch')).toHaveTextContent('feat/x')
    // The worktree path is a CopyablePath (click to copy).
    expect(screen.getByRole('button', { name: '/home/t/wt/abc' })).toHaveAttribute('title', 'Copy path')
  })

  it('shows — for an unknown branch and worktree', () => {
    render(<EnvironmentCard {...base} />)
    expect(screen.getByTestId('env-branch')).toHaveTextContent('—')
    expect(screen.getByTestId('env-worktree')).toHaveTextContent('—')
  })

  it('shows PR pills', () => {
    render(<EnvironmentCard {...base} prs={[pr(42)]} />)
    expect(screen.getByRole('link', { name: /#42/ })).toBeInTheDocument()
  })

  describe('PR auto-fix (D-14)', () => {
    const COPY = "PR Doctor fixes failing checks and conflicts on this session's PRs"

    it('shows the switch, with its copy, only when the session has a PR', () => {
      const prAutofix = { enabled: true, onChange: vi.fn() }
      const { rerender } = render(<EnvironmentCard {...base} prAutofix={prAutofix} />)
      expect(screen.queryByRole('switch', { name: 'PR auto-fix' })).toBeNull()
      rerender(<EnvironmentCard {...base} prs={[pr(1)]} prAutofix={prAutofix} />)
      const toggle = screen.getByRole('switch', { name: 'PR auto-fix' })
      expect(toggle).toHaveAttribute('aria-checked', 'true')
      expect(toggle).toHaveAccessibleDescription(COPY)
    })

    it('hands the flipped value to onChange', () => {
      const onChange = vi.fn()
      render(<EnvironmentCard {...base} prs={[pr(1)]} prAutofix={{ enabled: false, onChange }} />)
      fireEvent.click(screen.getByRole('switch', { name: 'PR auto-fix' }))
      expect(onChange).toHaveBeenCalledWith(true)
    })

    it('renders no switch when the caller wires none', () => {
      render(<EnvironmentCard {...base} prs={[pr(1)]} />)
      expect(screen.queryByRole('switch')).toBeNull()
    })
  })
})
