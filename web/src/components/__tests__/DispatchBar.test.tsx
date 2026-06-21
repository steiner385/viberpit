import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DispatchBar } from '../DispatchBar'
import { caretLineEdges } from '@/lib/caretLineEdges'
import type { GithubRepo } from '@/api/hooks'

const REPOS: GithubRepo[] = [
  { nameWithOwner: 'cairnea/KinDash', name: 'KinDash', owner: 'cairnea', isPrivate: true, url: '', defaultBranch: 'main', pushedAt: null, cloned: true },
  { nameWithOwner: 'acme/web', name: 'web', owner: 'acme', isPrivate: false, url: '', defaultBranch: 'main', pushedAt: null, cloned: false },
]

function makeProps(overrides: Partial<React.ComponentProps<typeof DispatchBar>> = {}) {
  return {
    repos: REPOS,
    reposLoading: false,
    reposError: false,
    onDispatch: vi.fn(),
    onNavigate: vi.fn(),
    onOpenHighlighted: vi.fn(),
    onRowAction: vi.fn(),
    onShowHelp: vi.fn(),
    onEscape: vi.fn(),
    isPending: false,
    ...overrides,
  }
}

describe('caretLineEdges', () => {
  it('single-line text is at both edges', () => {
    expect(caretLineEdges('hello', 2)).toEqual({ atFirstLine: true, atLastLine: true })
  })
  it('caret on first line of multi-line', () => {
    const v = 'a\nb\nc'
    expect(caretLineEdges(v, 0)).toEqual({ atFirstLine: true, atLastLine: false })
  })
  it('caret on last line of multi-line', () => {
    const v = 'a\nb\nc'
    expect(caretLineEdges(v, 5)).toEqual({ atFirstLine: false, atLastLine: true })
  })
  it('caret on a middle line is at neither edge', () => {
    const v = 'a\nb\nc'
    expect(caretLineEdges(v, 2)).toEqual({ atFirstLine: false, atLastLine: false })
  })
})

describe('DispatchBar', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.clearAllMocks()
  })

  it('renders a labelled prompt and a repo select', () => {
    render(<DispatchBar {...makeProps()} />)
    expect(screen.getByRole('textbox', { name: /new session prompt/i })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: /repository/i })).toBeInTheDocument()
  })

  it('Enter with text dispatches and clears', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn()
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'fix the bug')
    await user.keyboard('{Enter}')
    expect(onDispatch).toHaveBeenCalledWith({ initialPrompt: 'fix the bug', repo: null })
    expect(box).toHaveValue('')
  })

  it('Shift+Enter inserts a newline (does not dispatch)', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn()
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'line1')
    await user.keyboard('{Shift>}{Enter}{/Shift}')
    await user.type(box, 'line2')
    expect(onDispatch).not.toHaveBeenCalled()
    expect(box).toHaveValue('line1\nline2')
  })

  it('Enter on empty prompt opens the highlighted session', async () => {
    const user = userEvent.setup()
    const onOpenHighlighted = vi.fn()
    render(<DispatchBar {...makeProps({ onOpenHighlighted })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Enter}')
    expect(onOpenHighlighted).toHaveBeenCalledTimes(1)
  })

  it('ArrowDown on empty prompt navigates down', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    render(<DispatchBar {...makeProps({ onNavigate })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{ArrowDown}')
    expect(onNavigate).toHaveBeenCalledWith('down')
  })

  it('ArrowUp navigates only at the first line, not from a lower line', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    render(<DispatchBar {...makeProps({ onNavigate })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'a{Shift>}{Enter}{/Shift}b') // caret now on line 2
    await user.keyboard('{ArrowUp}')
    expect(onNavigate).not.toHaveBeenCalled() // moving cursor up within the textarea
  })

  it('Escape clears text and calls onEscape', async () => {
    const user = userEvent.setup()
    const onEscape = vi.fn()
    render(<DispatchBar {...makeProps({ onEscape })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'draft')
    await user.keyboard('{Escape}')
    expect(box).toHaveValue('')
    expect(onEscape).toHaveBeenCalled()
  })

  it('? opens the help overlay', async () => {
    const user = userEvent.setup()
    const onShowHelp = vi.fn()
    render(<DispatchBar {...makeProps({ onShowHelp })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Shift>}/{/Shift}')
    expect(onShowHelp).toHaveBeenCalled()
  })

  it('Alt+2 jumps to row index 1', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Alt>}2{/Alt}')
    expect(onRowAction).toHaveBeenCalledWith({ type: 'jump', index: 1 })
  })

  it('r on an empty prompt triggers rename action', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('r')
    expect(onRowAction).toHaveBeenCalledWith({ type: 'rename' })
  })

  it('r when the prompt already has text does NOT trigger rename', async () => {
    const user = userEvent.setup()
    const onRowAction = vi.fn()
    render(<DispatchBar {...makeProps({ onRowAction })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    // Type some text first, then press r — should NOT trigger rename since prompt is non-empty
    await user.type(box, 'fix ')
    await user.keyboard('r')
    expect(onRowAction).not.toHaveBeenCalled()
    expect(box).toHaveValue('fix r')
  })

  it('seeds the repo select from getLastRepo()', () => {
    window.localStorage.setItem('ccswitch.lastRepo', 'acme/web')
    render(<DispatchBar {...makeProps()} />)
    expect(screen.getByRole('combobox', { name: /repository/i })).toHaveValue('acme/web')
  })

  it('disables the select while repos load but keeps a persisted repo selected', () => {
    window.localStorage.setItem('ccswitch.lastRepo', 'acme/web')
    render(<DispatchBar {...makeProps({ repos: [], reposLoading: true })} />)
    const select = screen.getByRole('combobox', { name: /repository/i })
    expect(select).toBeDisabled()
    expect(select).toHaveValue('acme/web')
  })
})
