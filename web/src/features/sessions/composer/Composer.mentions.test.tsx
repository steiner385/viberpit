// `@` mentions in the SESSION composer (D-7): worktree files (async, debounced) in a
// Files group above a Sessions group. The assistant drawer stays sessions-only.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Composer } from './Composer'
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

// Every call's (id, q, enabled) — the debounce test reads the enabled q's.
const filesCalls: Array<{ id: string; q: string; enabled: boolean }> = []

vi.mock('@/api/hooks', () => ({
  useSessions: () => ({ data: SESSIONS }),
  useSessionFiles: (id: string, q: string, { enabled = true }: { enabled?: boolean } = {}) => {
    filesCalls.push({ id, q, enabled })
    return enabled
      ? { data: FILE_LIST, isFetching: false }
      : { data: undefined, isFetching: false }
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

beforeEach(() => { filesCalls.length = 0 })

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
    await user.keyboard('{Enter}')
    expect(box()).toHaveValue('look at @src/Composer.tsx ')
    expect((box() as HTMLTextAreaElement).selectionStart).toBe('look at @src/Composer.tsx '.length)
    expect(screen.queryByRole('listbox', { name: /mentions/i })).not.toBeInTheDocument()
  })

  it('choosing a session keeps today\'s [title](/sessions/<id>) token', async () => {
    const user = userEvent.setup()
    render(<SessionComposer rt={fakeRuntime('me')} />)
    await user.type(box(), '@comp')
    await screen.findByRole('option', { name: 'Composer refactor' })
    await user.keyboard('{ArrowDown}{ArrowDown}{Enter}')
    expect(box()).toHaveValue('[Composer refactor](/sessions/sess-2) ')
  })

  it('the textarea points aria-activedescendant at the highlighted option', async () => {
    const user = userEvent.setup()
    render(<SessionComposer rt={fakeRuntime('me')} />)
    await user.type(box(), '@comp')
    const first = await screen.findByRole('option', { name: 'src/Composer.tsx' })
    await waitFor(() => expect(box()).toHaveAttribute('aria-activedescendant', first.id))
    await user.keyboard('{Escape}')
    expect(box()).not.toHaveAttribute('aria-activedescendant')
  })

  it('does not open inside a slash command', async () => {
    const user = userEvent.setup()
    render(<SessionComposer rt={fakeRuntime('me')} />)
    await user.type(box(), '/review @comp')
    expect(screen.queryByRole('listbox', { name: /mentions/i })).not.toBeInTheDocument()
  })

  it('does not open inside a code span', async () => {
    const user = userEvent.setup()
    render(<SessionComposer rt={fakeRuntime('me')} />)
    await user.type(box(), 'run `foo @comp')
    expect(screen.queryByRole('listbox', { name: /mentions/i })).not.toBeInTheDocument()
  })

  it('debounces the files query: fast typing asks for "" then the settled query only', async () => {
    const user = userEvent.setup()
    render(<Composer {...defaults} fileMentionSessionId="me" />)
    await user.type(box(), '@comp')
    await waitFor(() => expect(filesCalls.some((c) => c.enabled && c.q === 'comp')).toBe(true))
    const asked = [...new Set(filesCalls.filter((c) => c.enabled).map((c) => c.q))]
    expect(asked).toEqual(['', 'comp'])
  })

  it('a session with no cwd gets no Files group (and no menu at all)', async () => {
    const user = userEvent.setup()
    render(<SessionComposer rt={fakeRuntime('nocwd')} />)
    await user.type(box(), '@comp')
    expect(screen.queryByRole('group', { name: 'Files' })).not.toBeInTheDocument()
    expect(screen.queryByRole('listbox', { name: /mentions/i })).not.toBeInTheDocument()
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
})
