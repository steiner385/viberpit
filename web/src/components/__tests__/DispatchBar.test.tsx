import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DispatchBar } from '../DispatchBar'
import { DispatchError } from '../dispatchError'
import { caretLineEdges } from '@/lib/caretLineEdges'
import type { GithubRepo } from '@/api/hooks'
import type { Account, ModelOption } from '@/api/types'

const REPOS: GithubRepo[] = [
  { nameWithOwner: 'cairnea/KinDash', name: 'KinDash', owner: 'cairnea', isPrivate: true, url: '', defaultBranch: 'main', pushedAt: null, cloned: true },
  { nameWithOwner: 'acme/web', name: 'web', owner: 'acme', isPrivate: false, url: '', defaultBranch: 'main', pushedAt: null, cloned: false },
]

const ACCOUNTS = [
  { name: 'work', nickname: 'Work' },
  { name: 'personal', nickname: null },
] as unknown as Account[]

const MODELS: ModelOption[] = [
  { id: 'default', label: 'Default' },
  { id: 'opus', label: 'Opus' },
  { id: 'fable', label: 'Fable' },
]

function makeProps(overrides: Partial<React.ComponentProps<typeof DispatchBar>> = {}) {
  return {
    repos: REPOS,
    reposLoading: false,
    reposError: false,
    accounts: ACCOUNTS,
    models: MODELS,
    onDispatch: vi.fn().mockResolvedValue(undefined),
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
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'fix the bug')
    await user.keyboard('{Enter}')
    expect(onDispatch).toHaveBeenCalledWith({ initialPrompt: 'fix the bug', repo: null, account: null, model: null, host: null, attachmentIds: [] })
    await waitFor(() => expect(box).toHaveValue(''))
  })

  it('shows a DispatchError inline, keeps the draft, and clears it on the next edit', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn().mockRejectedValue(new DispatchError('No usable accounts'))
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'retry me')
    await user.keyboard('{Enter}')
    expect(await screen.findByRole('alert')).toHaveTextContent('No usable accounts')
    expect(box).toHaveValue('retry me')
    await user.type(box, '!')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('says nothing for a rejection that is not a DispatchError (another surface owns it)', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn().mockRejectedValue(new Error('API error 409'))
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.click(box)
    await user.type(box, 'retry me')
    await user.keyboard('{Enter}')
    await waitFor(() => expect(onDispatch).toHaveBeenCalled())
    expect(box).toHaveValue('retry me')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('Shift+Enter inserts a newline (does not dispatch)', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn().mockResolvedValue(undefined)
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
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const create = screen.getByRole('button', { name: /create session/i })
    // Empty prompt → disabled (a "Create" button must never open an existing session)
    expect(create).toBeDisabled()
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.type(box, 'fix the bug')
    expect(create).toBeEnabled()
    await user.click(create)
    expect(onDispatch).toHaveBeenCalledWith({ initialPrompt: 'fix the bug', repo: null, account: null, model: null, host: null, attachmentIds: [] })
    await waitFor(() => expect(box).toHaveValue(''))
  })

  it('offers Account and Model dropdowns next to Repository, and forwards the picks', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)

    // All three selectors are present in the bar.
    expect(screen.getByRole('combobox', { name: /repository/i })).toBeInTheDocument()
    const accountSel = screen.getByRole('combobox', { name: /account/i })
    const modelSel = screen.getByRole('combobox', { name: /model/i })

    // Defaults: Auto-select account, account-default model.
    expect(accountSel).toHaveValue('')
    expect(modelSel).toHaveValue('default')

    await user.selectOptions(accountSel, 'work')
    await user.selectOptions(modelSel, 'opus')

    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.type(box, 'ship it')
    await user.keyboard('{Enter}')

    expect(onDispatch).toHaveBeenCalledWith({ initialPrompt: 'ship it', repo: null, account: 'work', model: 'opus', host: null, attachmentIds: [] })
  })

  it('normalizes the "Default" model to null so the backend keeps the account default', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)

    // Pick an account but leave Model on "Default".
    await user.selectOptions(screen.getByRole('combobox', { name: /account/i }), 'personal')
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.type(box, 'go')
    await user.keyboard('{Enter}')

    expect(onDispatch).toHaveBeenCalledWith({ initialPrompt: 'go', repo: null, account: 'personal', model: null, host: null, attachmentIds: [] })
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

  it('empty prompt + header highlighted: → expands, ← collapses, Enter toggles', async () => {
    const u = userEvent.setup()
    const onExpandHighlighted = vi.fn()
    const onCollapseHighlighted = vi.fn()
    const onToggleHighlighted = vi.fn()
    const onOpenHighlighted = vi.fn()
    render(<DispatchBar {...makeProps({
      activeDescendantIsHeader: true,
      onExpandHighlighted, onCollapseHighlighted, onToggleHighlighted, onOpenHighlighted,
    })} />)
    const ta = screen.getByLabelText('New session prompt')
    ta.focus()
    await u.keyboard('{ArrowRight}')
    expect(onExpandHighlighted).toHaveBeenCalled()
    expect(onOpenHighlighted).not.toHaveBeenCalled()
    await u.keyboard('{ArrowLeft}')
    expect(onCollapseHighlighted).toHaveBeenCalled()
    await u.keyboard('{Enter}')
    expect(onToggleHighlighted).toHaveBeenCalled()
  })

  it('empty prompt + session highlighted: → still opens (unchanged)', async () => {
    const u = userEvent.setup()
    const onOpenHighlighted = vi.fn()
    render(<DispatchBar {...makeProps({ activeDescendantIsHeader: false, onOpenHighlighted })} />)
    const ta = screen.getByLabelText('New session prompt')
    ta.focus()
    await u.keyboard('{ArrowRight}')
    expect(onOpenHighlighted).toHaveBeenCalled()
  })
})

describe('DispatchBar mobile collapse (More options)', () => {
  // Visibility is CSS-only (Tailwind `hidden sm:flex`), invisible to jsdom —
  // assert the class state + aria wiring here; real layout is Playwright territory.
  const moreBtn = () => screen.getByRole('button', { name: /more options/i })
  const accountWrap = () => screen.getByRole('combobox', { name: /account/i }).parentElement!
  const modelWrap = () => screen.getByRole('combobox', { name: /model/i }).parentElement!

  beforeEach(() => {
    window.localStorage.clear()
    vi.clearAllMocks()
  })

  it('starts collapsed: aria-expanded=false, account/model wrappers mobile-hidden', () => {
    render(<DispatchBar {...makeProps()} />)
    expect(moreBtn()).toHaveAttribute('aria-expanded', 'false')
    expect(accountWrap().className).toContain('hidden')
    expect(modelWrap().className).toContain('hidden')
  })

  it('toggle expands and collapses the account/model selects', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps()} />)
    await user.click(moreBtn())
    expect(moreBtn()).toHaveAttribute('aria-expanded', 'true')
    expect(accountWrap().className).not.toContain('hidden')
    expect(modelWrap().className).not.toContain('hidden')
    await user.click(moreBtn())
    expect(moreBtn()).toHaveAttribute('aria-expanded', 'false')
    expect(accountWrap().className).toContain('hidden')
  })

  it('shows an indicator only when collapsed with a non-default account or model', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps()} />)
    const indicator = () => screen.queryByText(/non-default account, model, or runner selected/i)
    // Collapsed but everything default → no indicator.
    expect(indicator()).not.toBeInTheDocument()
    await user.click(moreBtn())
    await user.selectOptions(screen.getByRole('combobox', { name: /account/i }), 'work')
    // Expanded → the select itself is visible, no indicator needed.
    expect(indicator()).not.toBeInTheDocument()
    await user.click(moreBtn())
    // Collapsed with a hidden non-default pick → indicator on.
    expect(indicator()).toBeInTheDocument()
  })

  it('non-default model also lights the indicator', async () => {
    const user = userEvent.setup()
    render(<DispatchBar {...makeProps()} />)
    await user.click(moreBtn())
    await user.selectOptions(screen.getByRole('combobox', { name: /model/i }), 'opus')
    await user.click(moreBtn())
    expect(screen.getByText(/non-default account, model, or runner selected/i)).toBeInTheDocument()
  })
})

describe('DispatchBar Runner dropdown', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.clearAllMocks()
  })

  it('includes the selected runner host in the dispatch payload', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch, hosts: [{ name: 'rocky' }, { name: 'dobby' }] })} />)

    // Runner dropdown is present
    const runnerSel = screen.getByRole('combobox', { name: /runner/i })
    expect(runnerSel).toBeInTheDocument()

    // Select dobby
    await user.selectOptions(runnerSel, 'dobby')

    // Type a prompt and submit
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.type(box, 'do a thing')
    await user.keyboard('{Enter}')

    expect(onDispatch).toHaveBeenCalledWith(
      expect.objectContaining({ host: 'dobby' })
    )
  })

  it('sends host=null when Runner is set to Auto', async () => {
    const user = userEvent.setup()
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch, hosts: [{ name: 'rocky' }] })} />)

    // Default is Auto — do not change the runner
    const box = screen.getByRole('textbox', { name: /new session prompt/i })
    await user.type(box, 'do a thing')
    await user.keyboard('{Enter}')

    expect(onDispatch).toHaveBeenCalledWith(
      expect.objectContaining({ host: null })
    )
  })

  it('does not render a Runner dropdown when hosts prop is absent', () => {
    render(<DispatchBar {...makeProps()} />)
    expect(screen.queryByRole('combobox', { name: /runner/i })).not.toBeInTheDocument()
  })

  it('does not render a Runner dropdown when hosts array is empty', () => {
    render(<DispatchBar {...makeProps({ hosts: [] })} />)
    expect(screen.queryByRole('combobox', { name: /runner/i })).not.toBeInTheDocument()
  })
})

// Inherited from `AccountsNickname.test.tsx`, where these ran against
// `NewSessionDialog`'s account select. That dialog was retired when creating a
// session became the dock's job; the bar's select is the surviving one and
// renders `a.nickname || a.name` the same way, so the coverage moved here with
// the behaviour rather than being deleted along with its old host.
describe('DispatchBar — account select uses nickname', () => {
  it('shows nickname as option label when one is set, and the raw name when not', () => {
    render(<DispatchBar {...makeProps()} />)

    const select = screen.getByRole('combobox', { name: 'Account' }) as HTMLSelectElement
    const options = Array.from(select.options).map((o) => ({ value: o.value, label: o.text }))

    expect(options.find((o) => o.value === 'work')?.label).toBe('Work')
    expect(options.find((o) => o.value === 'personal')?.label).toBe('personal')
  })

  it('option value stays the real account name even when a nickname is shown', () => {
    render(<DispatchBar {...makeProps()} />)

    const select = screen.getByRole('combobox', { name: 'Account' }) as HTMLSelectElement
    const work = Array.from(select.options).find((o) => o.value === 'work')
    // The dispatch payload keys off `value`, so a nickname must never reach it.
    expect(work?.text).toBe('Work')
    expect(work?.value).toBe('work')
  })
})
