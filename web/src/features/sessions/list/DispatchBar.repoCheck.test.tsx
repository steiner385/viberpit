import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { DispatchBar } from './DispatchBar'
import type { GithubRepo } from '@/api/hooks'
import type { Account, ModelOption } from '@/api/types'
import { setLastRepo } from '@/lib/lastRepo'

// ---------------------------------------------------------------------------
// Mocks — same block as DispatchBar.assess.test.tsx, plus the repo-check
// module (a plain function, deliberately NOT in @/api/hooks).
// ---------------------------------------------------------------------------
const { mockCheck } = vi.hoisted(() => ({ mockCheck: vi.fn() }))

vi.mock('@/api/hooks', () => ({
  useAssessDispatch: () => ({ assess: vi.fn().mockResolvedValue(null) }),
}))

vi.mock('@/api/repoCheck', () => ({
  checkRepo: mockCheck,
}))

vi.mock('@/features/sessions/composer/useImageAttachments', () => ({
  useImageAttachments: () => ({
    attachments: [],
    addFiles: vi.fn(),
    remove: vi.fn(),
    clear: vi.fn(),
    refs: () => [],
  }),
}))

vi.mock('@/features/sessions/composer/AttachmentChips', () => ({
  AttachmentChips: () => null,
}))

vi.mock('@/features/sessions/composer/MicButton', () => ({
  MicButton: () => null,
}))

vi.mock('@/lib/useDictationField', () => ({
  useDictationField: () => ({ supported: false, listening: false, error: null, toggle: vi.fn() }),
}))

vi.mock('@/lib/lastRepo', () => ({
  getLastRepo: () => 'steiner385/viberpit',
  setLastRepo: vi.fn(),
}))

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------
const REPOS: GithubRepo[] = [
  { nameWithOwner: 'steiner385/viberpit', name: 'viberpit', owner: 'steiner385', isPrivate: true, url: '', defaultBranch: 'master', pushedAt: null, cloned: true },
  { nameWithOwner: 'cairnea/KinDash', name: 'KinDash', owner: 'cairnea', isPrivate: true, url: '', defaultBranch: 'main', pushedAt: null, cloned: true },
]

const ACCOUNTS = [{ name: 'work', nickname: 'Work' }] as unknown as Account[]
const MODELS: ModelOption[] = [{ id: 'default', label: 'Default' }]

const PROMPT = 'fix the KinDash task modal'
const KINDASH = { suggested_repo: 'cairnea/KinDash', reason: 'mentions KinDash' }

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
    // Short timings so the tests run in real time; the defaults are for humans.
    repoCheckOptions: { debounceMs: 20, minChars: 5, submitWaitMs: 500 },
    ...overrides,
  }
}

async function typePrompt(user: ReturnType<typeof userEvent.setup>) {
  const box = screen.getByRole('textbox', { name: /new session prompt/i })
  await user.type(box, PROMPT)
  return box
}

const card = () => screen.queryByRole('status')

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
describe('DispatchBar — wrong-repository check', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.clearAllMocks()
    mockCheck.mockResolvedValue(null)
  })

  it('shows the card after typing goes idle, without holding anything', async () => {
    const user = userEvent.setup()
    mockCheck.mockResolvedValue(KINDASH)
    render(<DispatchBar {...makeProps()} />)
    await typePrompt(user)
    await waitFor(() => expect(card()).toBeInTheDocument())
    expect(card()).toHaveTextContent(/Looks like cairnea\/KinDash work — mentions KinDash/)
    expect(card()).not.toHaveTextContent(/Not created yet/)
    expect(mockCheck).toHaveBeenLastCalledWith(PROMPT, 'steiner385/viberpit')
  })

  it('Enter with a different-repo suggestion holds the dispatch and shows the card', async () => {
    const user = userEvent.setup()
    mockCheck.mockResolvedValue(KINDASH)
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = await typePrompt(user)
    await user.type(box, '{Enter}')
    await waitFor(() => expect(card()).toHaveTextContent(/Not created yet — Looks like cairnea\/KinDash work/))
    expect(onDispatch).not.toHaveBeenCalled()
    expect(box).toHaveValue(PROMPT)
    expect(screen.getByRole('button', { name: 'Switch repository to cairnea/KinDash' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Keep current repository' })).toHaveTextContent('Keep steiner385/viberpit')
  })

  it('Switch dispatches with the suggested repo, selects it and persists it', async () => {
    const user = userEvent.setup()
    mockCheck.mockResolvedValue(KINDASH)
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = await typePrompt(user)
    await user.type(box, '{Enter}')
    await waitFor(() => expect(card()).toHaveTextContent(/Not created yet/))

    await user.click(screen.getByRole('button', { name: 'Switch repository to cairnea/KinDash' }))

    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(onDispatch.mock.calls[0][0]).toMatchObject({ initialPrompt: PROMPT, repo: 'cairnea/KinDash' })
    expect(screen.getByRole('combobox', { name: /repository/i })).toHaveValue('cairnea/KinDash')
    expect(vi.mocked(setLastRepo)).toHaveBeenCalledWith('cairnea/KinDash')
    expect(card()).not.toBeInTheDocument()
    expect(box).toHaveValue('')
  })

  it('Keep dispatches with the original repo', async () => {
    const user = userEvent.setup()
    mockCheck.mockResolvedValue(KINDASH)
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = await typePrompt(user)
    await user.type(box, '{Enter}')
    await waitFor(() => expect(card()).toHaveTextContent(/Not created yet/))

    await user.click(screen.getByRole('button', { name: 'Keep current repository' }))

    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(onDispatch.mock.calls[0][0]).toMatchObject({ initialPrompt: PROMPT, repo: 'steiner385/viberpit' })
    expect(screen.getByRole('combobox', { name: /repository/i })).toHaveValue('steiner385/viberpit')
    expect(vi.mocked(setLastRepo)).not.toHaveBeenCalled()
    expect(card()).not.toBeInTheDocument()
  })

  it('Switch/Keep without a held dispatch only settles the repo', async () => {
    const user = userEvent.setup()
    mockCheck.mockResolvedValue(KINDASH)
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = await typePrompt(user)
    await waitFor(() => expect(card()).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Switch repository to cairnea/KinDash' }))

    expect(onDispatch).not.toHaveBeenCalled()
    expect(card()).not.toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: /repository/i })).toHaveValue('cairnea/KinDash')
    expect(box).toHaveValue(PROMPT)
    // Enter now goes straight through: the cached suggestion equals the selected repo.
    await user.type(box, '{Enter}')
    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(onDispatch.mock.calls[0][0]).toMatchObject({ repo: 'cairnea/KinDash' })
  })

  it('offers a suggested repo that is not in the repo list, and Switch selects it', async () => {
    const user = userEvent.setup()
    mockCheck.mockResolvedValue({ suggested_repo: 'cairnea/infra', reason: null })
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = await typePrompt(user)
    await user.type(box, '{Enter}')
    await waitFor(() => expect(card()).toHaveTextContent(/Looks like cairnea\/infra work/))
    // No reason ⇒ no trailing " — <reason>" after "work".
    expect(card()).not.toHaveTextContent(/work —/)

    await user.click(screen.getByRole('button', { name: 'Switch repository to cairnea/infra' }))

    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(onDispatch.mock.calls[0][0]).toMatchObject({ repo: 'cairnea/infra' })
    expect(screen.getByRole('combobox', { name: /repository/i })).toHaveValue('cairnea/infra')
  })

  it('dispatches immediately when the check returns no suggestion', async () => {
    const user = userEvent.setup()
    mockCheck.mockResolvedValue(null)
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = await typePrompt(user)
    await user.type(box, '{Enter}')
    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(onDispatch.mock.calls[0][0]).toMatchObject({ initialPrompt: PROMPT, repo: 'steiner385/viberpit' })
    expect(card()).not.toBeInTheDocument()
  })

  it('dispatches immediately when the suggestion is the selected repo', async () => {
    const user = userEvent.setup()
    mockCheck.mockResolvedValue({ suggested_repo: 'steiner385/viberpit', reason: 'already right' })
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = await typePrompt(user)
    await user.type(box, '{Enter}')
    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(card()).not.toBeInTheDocument()
  })

  it('dispatches immediately when the check rejects (fail-open)', async () => {
    const user = userEvent.setup()
    mockCheck.mockRejectedValue(new Error('network error'))
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = await typePrompt(user)
    await user.type(box, '{Enter}')
    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(card()).not.toBeInTheDocument()
  })

  it('dispatches after submitWaitMs when the check never answers (fail-open)', async () => {
    const user = userEvent.setup()
    mockCheck.mockReturnValue(new Promise(() => {}))
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = await typePrompt(user)
    await user.type(box, '{Enter}')
    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1), { timeout: 2000 })
  })

  it('a second Enter while held is the same as Keep', async () => {
    const user = userEvent.setup()
    mockCheck.mockResolvedValue(KINDASH)
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = await typePrompt(user)
    await user.type(box, '{Enter}')
    await waitFor(() => expect(card()).toHaveTextContent(/Not created yet/))
    expect(onDispatch).not.toHaveBeenCalled()

    await user.type(box, '{Enter}')

    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(onDispatch.mock.calls[0][0]).toMatchObject({ initialPrompt: PROMPT, repo: 'steiner385/viberpit' })
    expect(card()).not.toBeInTheDocument()
  })

  it('text typed while the check is pending is part of the dispatched prompt', async () => {
    const user = userEvent.setup()
    let answer: (v: null) => void = () => {}
    mockCheck.mockReturnValue(new Promise<null>((resolve) => { answer = resolve }))
    const onDispatch = vi.fn().mockResolvedValue(undefined)
    render(<DispatchBar {...makeProps({ onDispatch })} />)
    const box = await typePrompt(user)
    await user.type(box, '{Enter}')
    await user.type(box, ' now')
    answer(null)

    await waitFor(() => expect(onDispatch).toHaveBeenCalledTimes(1))
    expect(onDispatch.mock.calls[0][0]).toMatchObject({ initialPrompt: `${PROMPT} now` })
    expect(box).toHaveValue('')
  })

  it('editing the text drops the held state', async () => {
    const user = userEvent.setup()
    mockCheck.mockResolvedValue(KINDASH)
    render(<DispatchBar {...makeProps()} />)
    const box = await typePrompt(user)
    await user.type(box, '{Enter}')
    await waitFor(() => expect(card()).toHaveTextContent(/Not created yet/))
    await user.type(box, ' please')
    await waitFor(() => expect(card()).not.toHaveTextContent(/Not created yet/))
  })

  it('the card buttons are disabled while pending', async () => {
    const user = userEvent.setup()
    mockCheck.mockResolvedValue(KINDASH)
    const { rerender } = render(<DispatchBar {...makeProps()} />)
    await typePrompt(user)
    await waitFor(() => expect(card()).toBeInTheDocument())
    rerender(<DispatchBar {...makeProps({ isPending: true })} />)
    expect(screen.getByRole('button', { name: 'Switch repository to cairnea/KinDash' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Keep current repository' })).toBeDisabled()
  })
})
