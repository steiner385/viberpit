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
    onEnterList: vi.fn(),
    onJump: vi.fn(),
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
    expect(onDispatch).toHaveBeenCalledWith({ initialPrompt: 'fix the bug', repo: null, attachmentIds: [] })
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

  it('Create button is disabled until the prompt has text, then dispatches and clears', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn()
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const create = screen.getByRole('button', { name: /create session/i })
    // Empty prompt → disabled (a "Create" button must never open an existing session)
    expect(create).toBeDisabled()
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.type(box, 'fix the bug')
    expect(create).toBeEnabled()
    await user.click(create)
    expect(onDispatch).toHaveBeenCalledWith({ initialPrompt: 'fix the bug', repo: null, attachmentIds: [] })
    expect(box).toHaveValue('')
  })

  it('Create button never opens the highlighted session (stays disabled when empty)', async () => {
    const user = userEvent.setup()
    const onOpenHighlighted = vi.fn()
    render(<DispatchBar {...makeProps({ onOpenHighlighted })} />)
    const create = screen.getByRole('button', { name: /create session/i })
    expect(create).toBeDisabled()
    await user.click(create)
    expect(onOpenHighlighted).not.toHaveBeenCalled()
  })

  it('Create button is disabled while a session is pending', () => {
    render(<DispatchBar {...makeProps({ isPending: true })} />)
    expect(screen.getByRole('button', { name: /create session/i })).toBeDisabled()
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

  it('? opens the help overlay when the prompt is empty', async () => {
    const user = userEvent.setup()
    const onShowHelp = vi.fn()
    render(<DispatchBar {...makeProps({ onShowHelp })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Shift>}/{/Shift}')
    expect(onShowHelp).toHaveBeenCalled()
  })

  it('? is typed literally (does NOT open help) when the prompt has text', async () => {
    const user = userEvent.setup()
    const onShowHelp = vi.fn()
    render(<DispatchBar {...makeProps({ onShowHelp })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'why is the sky blue?')
    expect(onShowHelp).not.toHaveBeenCalled()
    expect((box as HTMLTextAreaElement).value).toBe('why is the sky blue?')
  })

  it('Alt+2 jumps to row index 1', async () => {
    const user = userEvent.setup()
    const onJump = vi.fn()
    render(<DispatchBar {...makeProps({ onJump })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Alt>}2{/Alt}')
    expect(onJump).toHaveBeenCalledWith(1)
  })

  it('Tab on an empty prompt enters list mode', async () => {
    const user = userEvent.setup()
    const onEnterList = vi.fn()
    render(<DispatchBar {...makeProps({ onEnterList })} />)
    await user.click(screen.getByRole('textbox', { name: /new session prompt/i }))
    await user.keyboard('{Tab}')
    expect(onEnterList).toHaveBeenCalledTimes(1)
  })

  it('Tab with a draft does NOT enter list mode (native focus move)', async () => {
    const user = userEvent.setup()
    const onEnterList = vi.fn()
    render(<DispatchBar {...makeProps({ onEnterList })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'draft')
    await user.keyboard('{Tab}')
    expect(onEnterList).not.toHaveBeenCalled()
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

  it('ArrowUp/ArrowDown with single-line text navigate the list (no caret destination)', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    render(<DispatchBar {...makeProps({ onNavigate })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.type(box, 'fix the bug')        // single line, caret at end
    await user.keyboard('{ArrowUp}')
    expect(onNavigate).toHaveBeenCalledWith('up')
    await user.keyboard('{ArrowDown}')
    expect(onNavigate).toHaveBeenCalledWith('down')
  })
})
