// `@` mentions in the SESSION composer (D-7): worktree files (async, debounced) in a
// Files group above a Sessions group. The assistant drawer stays sessions-only.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within, waitFor, fireEvent, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createRef } from 'react'
import { Composer, type ComposerAttach } from './Composer'
import { SessionComposer } from './SessionComposer'
import type { SessionRuntime } from '@/session/useSessionRuntime'

const SESSIONS = [
  { id: 'me', name: 'This session', cwd: '/w/me', engine: 'claude' },
  { id: 'sess-2', name: 'Composer refactor', cwd: '/w/two', engine: 'claude' },
  { id: 'nocwd', name: 'No worktree', cwd: null, engine: 'claude' },
]
const FILES = ['src/Composer.tsx', 'src/composer/util.ts']
// One stable object, like react-query's structurally-shared `data`.
const FILE_LIST = { files: FILES.map((path) => ({ path })), truncated: false }
const LOADED = { data: FILE_LIST, isFetching: false }
const LOADING = { data: undefined, isFetching: true }
type FilesResponse = { data: typeof FILE_LIST | undefined; isFetching: boolean; isPlaceholderData?: boolean }

// What an enabled files query returns; a test can swap it (loading, other paths), or
// answer per query (`filesFor`).
let filesResponse: FilesResponse = LOADED
let filesFor: ((q: string) => FilesResponse) | null = null
// Every call's (id, q, enabled, at) — the debounce test reads the enabled q's.
const filesCalls: Array<{ id: string; q: string; enabled: boolean; at: number }> = []

vi.mock('@/api/hooks', () => ({
  useSessions: () => ({ data: SESSIONS }),
  useSessionFiles: (id: string, q: string, { enabled = true }: { enabled?: boolean } = {}) => {
    filesCalls.push({ id, q, enabled, at: Date.now() })
    if (!enabled) return { data: undefined, isFetching: false }
    return filesFor ? filesFor(q) : filesResponse
  },
  useGenerateSuggestions: () => ({ mutateAsync: vi.fn() }),
  useEngineCapabilities: () => undefined,
  useModels: () => ({ data: undefined }),
}))
vi.mock('./PromptRow', () => ({ PromptRow: () => null }))

function fakeRuntime(sessionId: string): SessionRuntime {
  return {
    sessionId, mode: 'default', onPrompt: vi.fn(), onInterrupt: vi.fn(), onSetMode: vi.fn(),
    onCancelQueued: vi.fn(), onCommand: vi.fn(), commands: [], chatArmed: false, suggestions: [],
    state: { status: { state: 'idle', count: 0 }, queued: [], turns: [] },
  } as unknown as SessionRuntime
}

const defaults = { status: { state: 'idle', count: 0 }, onPrompt: vi.fn(), onInterrupt: vi.fn() }
const box = () => screen.getByRole('textbox', { name: /message/i })
const enabledQs = () => [...new Set(filesCalls.filter((c) => c.enabled).map((c) => c.q))]
// The Files rows answer the typed query: the debounce settled and the fetch landed.
const filesSettled = () => waitFor(() =>
  expect(screen.getByRole('group', { name: 'Files' })).not.toHaveAttribute('aria-busy'))

beforeEach(() => { filesCalls.length = 0; filesResponse = LOADED; filesFor = null; vi.clearAllMocks() })

describe('session composer @ menu — files and sessions (D-7)', () => {
  it('@comp lists Files above Sessions, and leaves the current session out', async () => {
    const user = userEvent.setup()
    render(<SessionComposer rt={fakeRuntime('me')} />)
    await user.type(box(), '@comp')
    const listbox = await screen.findByRole('listbox', { name: /mentions/i })
    const files = within(listbox).getByRole('group', { name: 'Files' })
    const sessions = within(listbox).getByRole('group', { name: 'Sessions' })
    expect(files.compareDocumentPosition(sessions) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(within(files).getByRole('option', { name: 'src/Composer.tsx' })).toBeInTheDocument()
    expect(within(sessions).getByRole('option', { name: 'Composer refactor' })).toBeInTheDocument()
    expect(within(listbox).queryByText('This session')).not.toBeInTheDocument()
    expect(filesCalls.filter((c) => c.enabled).every((c) => c.id === 'me')).toBe(true)
  })

  it('Enter on a file inserts @<path> and one space at the caret', async () => {
    const user = userEvent.setup()
    render(<SessionComposer rt={fakeRuntime('me')} />)
    await user.type(box(), 'look at @comp')
    await screen.findByRole('option', { name: 'src/Composer.tsx' })
    await filesSettled()
    await user.keyboard('{Enter}')
    expect(box()).toHaveValue('look at @src/Composer.tsx ')
    expect((box() as HTMLTextAreaElement).selectionStart).toBe('look at @src/Composer.tsx '.length)
    expect(screen.queryByRole('listbox', { name: /mentions/i })).not.toBeInTheDocument()
  })

  it('a path with whitespace goes in quoted, @"<path>" — the CLI\'s quoted @ form', async () => {
    const user = userEvent.setup()
    const spaced = { files: [{ path: 'docs/My Notes.md' }], truncated: false }
    filesResponse = { data: spaced, isFetching: false }
    render(<SessionComposer rt={fakeRuntime('me')} />)
    await user.type(box(), '@notes')
    await screen.findByRole('option', { name: 'docs/My Notes.md' })
    await filesSettled()
    await user.keyboard('{Enter}')
    expect(box()).toHaveValue('@"docs/My Notes.md" ')
  })

  it('choosing a session keeps today\'s [title](/sessions/<id>) token', async () => {
    const user = userEvent.setup()
    render(<SessionComposer rt={fakeRuntime('me')} />)
    await user.type(box(), '@comp')
    await screen.findByRole('option', { name: 'Composer refactor' })
    await filesSettled()
    await user.keyboard('{ArrowDown}{ArrowDown}{Enter}')
    expect(box()).toHaveValue('[Composer refactor](/sessions/sess-2) ')
  })

  it('while open, the textarea owns the listbox: aria-controls/autocomplete/activedescendant', async () => {
    const user = userEvent.setup()
    render(<SessionComposer rt={fakeRuntime('me')} />)
    // aria-expanded is deliberately absent: axe's aria-allowed-attr rejects it on a textbox.
    expect(box()).not.toHaveAttribute('aria-expanded')
    expect(box()).not.toHaveAttribute('aria-controls')
    await user.type(box(), '@comp')
    const listbox = await screen.findByRole('listbox', { name: /mentions/i })
    const first = within(listbox).getByRole('option', { name: 'src/Composer.tsx' })
    await waitFor(() => expect(box()).toHaveAttribute('aria-activedescendant', first.id))
    expect(listbox.id).not.toBe('')
    expect(box()).toHaveAttribute('aria-controls', listbox.id)
    expect(box()).toHaveAttribute('aria-autocomplete', 'list')
    await user.keyboard('{Escape}')
    expect(box()).not.toHaveAttribute('aria-activedescendant')
    expect(box()).not.toHaveAttribute('aria-controls')
  })

  it('Enter while files are still loading (no session match) sends nothing and keeps the menu open', async () => {
    const user = userEvent.setup()
    filesResponse = LOADING
    const rt = fakeRuntime('me')
    render(<SessionComposer rt={rt} />)
    await user.type(box(), '@zzz')
    expect(screen.getByRole('listbox', { name: /mentions/i })).toHaveTextContent(/searching files/i)
    await user.keyboard('{Enter}')
    expect(rt.onPrompt).not.toHaveBeenCalled()
    expect(box()).toHaveValue('@zzz')
    expect(screen.getByRole('listbox', { name: /mentions/i })).toBeInTheDocument()
  })

  it('Enter on the previous query\'s rows (results not caught up) inserts nothing and sends nothing', async () => {
    const user = userEvent.setup()
    const RECENT = { files: [{ path: 'docs/recent.md' }], truncated: false }
    // `@` lists the recent files; `comp` is still in flight, so react-query keeps the
    // recent rows up as placeholder data.
    filesFor = (q) => (q === ''
      ? { data: RECENT, isFetching: false }
      : { data: RECENT, isFetching: true, isPlaceholderData: true })
    const rt = fakeRuntime('me')
    render(<SessionComposer rt={rt} />)
    await user.type(box(), '@')
    await screen.findByRole('option', { name: 'docs/recent.md' })
    // Typed fast: Enter lands inside the debounce window, before `comp` is even asked.
    await user.type(box(), 'comp{Enter}')
    expect(box()).toHaveValue('@comp')
    expect(rt.onPrompt).not.toHaveBeenCalled()
    // Debounce settled, fetch still in flight (placeholder rows): still waits.
    await waitFor(() => expect(enabledQs()).toContain('comp'))
    expect(screen.getByRole('group', { name: 'Files' })).toHaveAttribute('aria-busy', 'true')
    await user.keyboard('{Enter}')
    await user.keyboard('{Tab}')
    expect(box()).toHaveValue('@comp')
    expect(rt.onPrompt).not.toHaveBeenCalled()
    expect(screen.getByRole('listbox', { name: /mentions/i })).toBeInTheDocument()
    // The stale rows are dimmed.
    expect(screen.getByRole('option', { name: 'docs/recent.md' })).toHaveClass('text-ink-muted')
  })

  it('opens in a slash command\'s args — /review @src/… (Claude Code takes @path there)', async () => {
    const user = userEvent.setup()
    render(<SessionComposer rt={fakeRuntime('me')} />)
    await user.type(box(), '/review @comp')
    expect(await screen.findByRole('group', { name: 'Files' })).toBeInTheDocument()
    await filesSettled()
    await user.keyboard('{Enter}')
    expect(box()).toHaveValue('/review @src/Composer.tsx ')
  })

  it('does not open inside a code span', async () => {
    const user = userEvent.setup()
    render(<SessionComposer rt={fakeRuntime('me')} />)
    await user.type(box(), 'run `foo @comp')
    expect(screen.queryByRole('listbox', { name: /mentions/i })).not.toBeInTheDocument()
  })

  describe('debounce (fake timers)', () => {
    beforeEach(() => { vi.useFakeTimers() })
    afterEach(() => { vi.useRealTimers() })

    it('fast typing issues at most one files query per 120 ms', () => {
      render(<Composer {...defaults} fileMentionSessionId="me" />)
      // A keystroke every 30 ms — four inside one debounce window.
      for (const v of ['@', '@c', '@co', '@com', '@comp']) {
        fireEvent.change(box(), { target: { value: v, selectionStart: v.length } })
        act(() => { vi.advanceTimersByTime(30) })
      }
      expect(enabledQs()).toEqual([''])
      act(() => { vi.advanceTimersByTime(120) })
      expect(enabledQs()).toEqual(['', 'comp'])
      const firstAt = (q: string) => filesCalls.find((c) => c.enabled && c.q === q)!.at
      expect(firstAt('comp') - firstAt('')).toBeGreaterThanOrEqual(120)
    })
  })

  it('a session with no cwd gets a sessions-only menu — no Files group, no files query', async () => {
    const user = userEvent.setup()
    render(<SessionComposer rt={fakeRuntime('nocwd')} />)
    await user.type(box(), '@comp')
    const listbox = screen.getByRole('listbox', { name: /session mentions/i })
    expect(within(listbox).getByRole('option', { name: 'Composer refactor' })).toBeInTheDocument()
    expect(within(listbox).queryByText('No worktree')).not.toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Files' })).not.toBeInTheDocument()
    expect(filesCalls.some((c) => c.enabled)).toBe(false)
  })

  it('the assistant composer shows sessions only — no Files group, no files query', async () => {
    const user = userEvent.setup()
    render(
      <SessionComposer rt={fakeRuntime('me')} hideSessionControls
        mentionSource={[{ id: 'sess-2', title: 'Composer refactor' }]} />,
    )
    await user.type(box(), '@comp')
    const listbox = screen.getByRole('listbox', { name: /session mentions/i })
    expect(within(listbox).getByText('Composer refactor')).toBeInTheDocument()
    expect(within(listbox).queryByRole('group', { name: 'Files' })).not.toBeInTheDocument()
    expect(filesCalls.some((c) => c.enabled)).toBe(false)
  })

  it('the assistant composer follows the same slash rule: mentions open in command args', async () => {
    const user = userEvent.setup()
    render(
      <SessionComposer rt={fakeRuntime('me')} hideSessionControls
        mentionSource={[{ id: 'sess-2', title: 'Composer refactor' }]} />,
    )
    await user.type(box(), '/ask @comp')
    expect(screen.getByRole('listbox', { name: /session mentions/i })).toHaveTextContent('Composer refactor')
  })
})

// D-7: a transcript path's "Attach as @" reaches the dock composer through
// `attachRef` (the shell bridge in `shell/sessionGestures`), with the same quoting
// the `@` menu uses.
describe('attachFile — a transcript path attached as @ (D-7)', () => {
  it('appends @<path> after the draft, separated by one space', async () => {
    const user = userEvent.setup()
    const ref = createRef<ComposerAttach>()
    render(<Composer {...defaults} attachRef={ref} />)
    await user.type(box(), 'look at')
    act(() => ref.current!.attachFile('src/a.ts'))
    expect(box()).toHaveValue('look at @src/a.ts ')
    expect(box()).toHaveFocus()
  })

  it('quotes a path with whitespace, like the @ menu', () => {
    const ref = createRef<ComposerAttach>()
    render(<Composer {...defaults} attachRef={ref} />)
    act(() => ref.current!.attachFile('docs/a b.md'))
    expect(box()).toHaveValue('@"docs/a b.md" ')
  })

  it('the SessionComposer forwards it to the Composer', () => {
    const ref = createRef<ComposerAttach>()
    render(<SessionComposer rt={fakeRuntime('me')} attachRef={ref} />)
    act(() => ref.current!.attachFile('src/a.ts'))
    expect(box()).toHaveValue('@src/a.ts ')
  })
})

// D-9: Edit & resend replaces the draft with the cut prompt, through the same handle,
// and reads it first to warn that unsent text will be replaced.
describe('setDraft / draftText — Edit & resend (D-9)', () => {
  it('reads the draft and replaces it, caret at the end', async () => {
    const user = userEvent.setup()
    const ref = createRef<ComposerAttach>()
    render(<Composer {...defaults} attachRef={ref} />)
    await user.type(box(), 'half-written')
    expect(ref.current!.draftText()).toBe('half-written')
    act(() => ref.current!.setDraft('fix the bug'))
    expect(box()).toHaveValue('fix the bug')
    expect(ref.current!.draftText()).toBe('fix the bug')
    expect((box() as HTMLTextAreaElement).selectionStart).toBe('fix the bug'.length)
  })
})
