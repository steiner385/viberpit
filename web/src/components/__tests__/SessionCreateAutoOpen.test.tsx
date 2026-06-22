import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Dashboard } from '../Dashboard'
import * as hooks from '@/api/hooks'
import type { Session } from '@/api/types'

// ── Mock @/api/hooks (same scaffold as KeyboardSessions.test.tsx) ─────────────

vi.mock('@/api/hooks', () => ({
  useAccounts: vi.fn(),
  useSessions: vi.fn(),
  useCreateSession: vi.fn(),
  useRenameSession: vi.fn(),
  useCloseSession: vi.fn(),
  useRefreshAccount: vi.fn(),
  useUpdateAccountNickname: vi.fn(),
  useDeleteAccount: vi.fn(),
  useGithubRepos: vi.fn(),
  useGithubBranches: vi.fn(),
  useSettings: vi.fn(),
  useUpdateSettings: vi.fn(),
  useReauthAccount: vi.fn(),
  useReauthStatus: vi.fn(),
  useCancelReauth: vi.fn(),
  useArchivedSessions: vi.fn(),
  useUnarchiveSession: vi.fn(),
  usePermanentDeleteSession: vi.fn(),
  QK: {
    accounts: ['accounts'],
    sessions: ['sessions'],
    archived: ['archived-sessions'],
    config: ['config'],
    githubRepos: ['github-repos'],
    githubBranches: (repo?: string) => ['github-branches', repo],
    settings: ['settings'],
  },
}))

const createdSession: Session = {
  id: 'new-sess', name: 'New Session', accountName: 'acct-a',
  status: 'idle', createdAt: '2026-06-21T00:00:00Z', updatedAt: '2026-06-21T00:00:00Z',
}

function setupHooks() {
  vi.mocked(hooks.useAccounts).mockReturnValue({ data: [{ name: 'acct-a' }] } as never)
  vi.mocked(hooks.useSessions).mockReturnValue({ data: [] } as never)
  vi.mocked(hooks.useGithubRepos).mockReturnValue({ data: [] } as never)
  vi.mocked(hooks.useGithubBranches).mockReturnValue({ data: { default: null, branches: [] } } as never)
  vi.mocked(hooks.useSettings).mockReturnValue({ data: { defaultPermissionMode: 'interactive', thinkingCollapsed: false, repoRoot: null } } as never)
  vi.mocked(hooks.useArchivedSessions).mockReturnValue({ data: [] } as never)
  vi.mocked(hooks.useCreateSession).mockReturnValue({
    mutate: (input: hooks.CreateSessionInput, opts?: { onSuccess?: (s: Session) => void }) => {
      opts?.onSuccess?.(createdSession)
    },
    isPending: false,
  } as never)
  // Stub the remaining mutation hooks as no-op mutate/isPending objects.
  const noopMut = { mutate: vi.fn(), isPending: false } as never
  vi.mocked(hooks.useRenameSession).mockReturnValue(noopMut)
  vi.mocked(hooks.useCloseSession).mockReturnValue(noopMut)
  vi.mocked(hooks.useRefreshAccount).mockReturnValue(noopMut)
  vi.mocked(hooks.useUpdateAccountNickname).mockReturnValue(noopMut)
  vi.mocked(hooks.useDeleteAccount).mockReturnValue(noopMut)
  vi.mocked(hooks.useReauthAccount).mockReturnValue(noopMut)
  vi.mocked(hooks.useReauthStatus).mockReturnValue({ data: undefined } as never)
  vi.mocked(hooks.useCancelReauth).mockReturnValue(noopMut)
  vi.mocked(hooks.useUnarchiveSession).mockReturnValue(noopMut)
  vi.mocked(hooks.usePermanentDeleteSession).mockReturnValue(noopMut)
  // useUpdateSettings is referenced by SettingsPanel but may not be called at render time
  vi.mocked(hooks.useUpdateSettings).mockReturnValue(noopMut)
}

describe('Dashboard — auto-open on create', () => {
  beforeEach(() => vi.clearAllMocks())

  it('opens the new session with its prompt after dialog submit', async () => {
    const onOpenSession = vi.fn()
    setupHooks()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Dashboard onOpenSession={onOpenSession} onReviewSession={() => {}} focusDispatchSignal={0} />
      </QueryClientProvider>,
    )
    // Open the New Session dialog — click the "New" button in the header (aria-label="New session")
    await userEvent.click(screen.getByRole('button', { name: 'New session' }))
    // Type the prompt — label is "Initial prompt (optional)" — the textarea id is "new-session-prompt"
    const promptField = screen.getByLabelText(/initial prompt/i)
    await userEvent.type(promptField, 'kick things off')
    // Submit — button text is "Create"
    await userEvent.click(screen.getByRole('button', { name: /^create$/i }))

    expect(onOpenSession).toHaveBeenCalledWith('new-sess', 'kick things off')
  })
})
