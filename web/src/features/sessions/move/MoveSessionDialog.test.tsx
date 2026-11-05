/**
 * "Move to host…" dialog (D-17): the target select, the preflight's refusals in plain
 * language, the dirty-tree consent (private repos only — revised D1), the background
 * confirm, and the three outcomes. The API is mocked at `api.get` / `api.post`; the
 * real `ApiRequestError` and `apiErrorCode` decode the failures.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiRequestError } from '@/api/client'
import { FLEET_HOSTS_KEY, type FleetHost } from '@/api/fleet'
import type { MovePreflight } from '@/api/types'
import MoveSessionDialog, { DIRTY_LIST_CAP } from './MoveSessionDialog'
import { CARRY_DIRTY_COPY, MOVE_REASON_COPY, PUBLIC_REPO_COPY } from './moveReasons'

// `get` is the PREFLIGHT mock; the fleet list (refetched on mount) answers from `fleet`.
const get = vi.fn()
const post = vi.fn()
let fleet: FleetHost[] = []
vi.mock('@/api/client', async (importOriginal) => {
  const real = await importOriginal<typeof import('@/api/client')>()
  return {
    ...real,
    api: {
      get: (url: string) => (url === '/fleet/hosts' ? Promise.resolve({ hosts: fleet }) : get(url)),
      post: (...a: unknown[]) => post(...a),
    },
  }
})

const host = (name: string, over: Partial<FleetHost> = {}): FleetHost => ({
  name, endpoint: `https://${name}:8790`, enabled: true, weight: 1, maxSessions: null, healthy: true,
  activeSessions: 0, loadAvg: null, memAvailableMb: null, sha: null, checkoutSha: null, protocol: null,
  lastProbeAt: null, lastError: null, sshHost: null, repoPath: null, upgrade: null, ...over,
})

const preflight = (over: Partial<MovePreflight> = {}): MovePreflight => ({
  sessionId: 's1', source: 'dobby',
  hosts: [{ name: 'rocky', ok: true, reason: null, message: '' }],
  dirty: { count: 0, paths: [] }, repoPrivate: null, backgroundTasks: [], factsError: null,
  target: 'rocky', verdict: { ok: true, reason: null, message: '' }, ...over,
})

function renderDialog(hosts: FleetHost[] = [host('dobby'), host('rocky'), host('off', { enabled: false })]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  fleet = hosts
  qc.setQueryData(FLEET_HOSTS_KEY, { hosts })
  const onClose = vi.fn()
  const onSettled = vi.fn()
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  )
  render(<MoveSessionDialog sessionId="s1" source="dobby" onClose={onClose} onSettled={onSettled} />, { wrapper })
  return { onClose, onSettled }
}

const moveButton = () => screen.getByRole('button', { name: 'Move' })

async function pick(name = 'rocky') {
  await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Target host' }), name)
}

beforeEach(() => {
  get.mockReset()
  post.mockReset()
})

describe('MoveSessionDialog — target select', () => {
  it('lists enabled hosts other than the current one, and fetches no preflight until one is picked', () => {
    renderDialog()
    const options = screen.getAllByRole('option').map((o) => o.textContent)
    expect(options).toEqual(['Choose a host…', 'rocky'])
    expect(get).not.toHaveBeenCalled()
    expect(moveButton()).toBeDisabled()
  })

  it('picking a host runs the preflight for it; an ok verdict enables Move', async () => {
    get.mockResolvedValue(preflight())
    renderDialog()
    await pick()
    expect(get).toHaveBeenCalledWith('/sessions/s1/move/preflight?host=rocky')
    await waitFor(() => expect(moveButton()).toBeEnabled())
    expect(screen.queryByTestId('move-refusal')).toBeNull()
  })
})

describe('MoveSessionDialog — refusals in plain language (Review Focus 4)', () => {
  it.each([
    ['MOVE_MID_TURN', /Wait for the turn to finish or Interrupt first/],
    ['MOVE_ENGINE_UNSUPPORTED', /Codex history stays on its host/],
    ['MOVE_HAS_CHILDREN', /steering live child sessions/],
    ['MOVE_IS_CHILD', /steered by a live parent/],
    ['MOVE_AWAITING_ANSWER', /open question, permission or plan card/],
  ])('%s shows its reason and keeps Move off', async (reason, text) => {
    get.mockResolvedValue(preflight({ verdict: { ok: false, reason, message: 'server words' } }))
    renderDialog()
    await pick()
    expect(await screen.findByTestId('move-refusal')).toHaveTextContent(text)
    expect(moveButton()).toBeDisabled()
  })

  it('every contract code has its own copy', () => {
    for (const code of [
      'MOVE_DISABLED', 'MOVE_NO_FLEET', 'MOVE_NOT_FOUND', 'MOVE_ENGINE_UNSUPPORTED', 'MOVE_ASSISTANT',
      'MOVE_NATIVE_DISPLAY', 'MOVE_IN_FLIGHT', 'MOVE_INACTIVE', 'MOVE_NOT_STARTED', 'MOVE_MID_TURN',
      'MOVE_AWAITING_ANSWER', 'MOVE_HAS_CHILDREN', 'MOVE_IS_CHILD', 'MOVE_NOT_MANAGED_WORKTREE',
      'MOVE_UNKNOWN_HOST', 'MOVE_SAME_HOST', 'MOVE_TARGET_DISABLED', 'MOVE_TARGET_UNHEALTHY',
      'MOVE_TARGET_ENGINE', 'MOVE_TARGET_PLATFORM', 'MOVE_TARGET_FULL', 'MOVE_TARGET_UNAVAILABLE',
      'MOVE_DIRTY_NEEDS_CONSENT', 'MOVE_DIRTY_PUBLIC_REPO', 'MOVE_BACKGROUND_NEEDS_CONFIRM',
      'MOVE_SOURCE_UNREACHABLE', 'MOVE_EXPORT_FAILED', 'MOVE_RESTAMP_FAILED', 'MOVE_IMPORT_FAILED',
      'MOVE_TRANSCRIPT_STALE', 'MOVE_RESUME_FAILED', 'MOVE_LEASE_HELD', 'MOVE_PATH_MISMATCH',
      'MOVE_WORKTREE_MISSING', 'MOVE_NOT_STAMPED', 'MOVE_FINALIZE_REFUSED',
    ]) expect(MOVE_REASON_COPY[code], code).toBeTruthy()
  })

  it('an unreachable source (factsError) is a refusal', async () => {
    get.mockResolvedValue(preflight({ factsError: 'MOVE_SOURCE_UNREACHABLE', dirty: null }))
    renderDialog()
    await pick()
    expect(await screen.findByTestId('move-refusal')).toHaveTextContent(MOVE_REASON_COPY.MOVE_SOURCE_UNREACHABLE)
    expect(moveButton()).toBeDisabled()
  })

  it('a refused preflight request (403 for a token) shows its reason', async () => {
    get.mockRejectedValue(new ApiRequestError(403, { detail: { code: 'SESSION_CONTROL_HUMAN_ONLY', message: 'x' } }))
    renderDialog()
    await pick()
    expect(await screen.findByTestId('move-refusal')).toHaveTextContent('Only a person can move a session.')
  })

  it('a host the last preflight refused is greyed out with its reason', async () => {
    get.mockResolvedValue(preflight({
      hosts: [{ name: 'rocky', ok: true, reason: null, message: '' },
        { name: 'win11', ok: false, reason: 'MOVE_TARGET_FULL', message: '' }],
    }))
    renderDialog([host('dobby'), host('rocky'), host('win11')])
    await pick()
    await waitFor(() => expect(screen.getByRole('option', { name: /win11 — That host is at its session limit/ })).toBeDisabled())
  })
})

describe('MoveSessionDialog — a dirty tree', () => {
  const paths = Array.from({ length: 12 }, (_, i) => `src/file-${i}.ts`)

  it('private repo: lists the paths (capped, +N more) and needs the consent box before Move', async () => {
    get.mockResolvedValue(preflight({ dirty: { count: 60, paths }, repoPrivate: true }))
    post.mockResolvedValue({ sessionId: 's1', from: 'dobby', to: 'rocky', outcome: 'moved', finalized: true, recoveryRef: null, session: null })
    renderDialog()
    await pick()
    const list = await screen.findByTestId('move-dirty-paths')
    expect(list.querySelectorAll('li')).toHaveLength(DIRTY_LIST_CAP + 1)
    expect(list).toHaveTextContent(`+${60 - DIRTY_LIST_CAP} more`)
    const consent = screen.getByRole('checkbox', { name: CARRY_DIRTY_COPY })
    expect(moveButton()).toBeDisabled()
    await userEvent.click(consent)
    expect(moveButton()).toBeEnabled()
    await userEvent.click(moveButton())
    expect(post).toHaveBeenCalledWith('/sessions/s1/move', { host: 'rocky', carryDirty: true, stopBackgroundTasks: false })
  })

  it('public repo: the public copy, no consent box, Move stays off', async () => {
    get.mockResolvedValue(preflight({
      dirty: { count: 2, paths: ['a.py', 'b.py'] }, repoPrivate: false,
      verdict: { ok: false, reason: 'MOVE_DIRTY_PUBLIC_REPO', message: 'server words' },
    }))
    renderDialog()
    await pick()
    expect(await screen.findByTestId('move-refusal')).toHaveTextContent(PUBLIC_REPO_COPY)
    expect(screen.queryByRole('checkbox', { name: CARRY_DIRTY_COPY })).toBeNull()
    expect(moveButton()).toBeDisabled()
  })

  it('unknown visibility is treated as public even when the verdict says ok', async () => {
    get.mockResolvedValue(preflight({ dirty: { count: 1, paths: ['a.py'] }, repoPrivate: null }))
    renderDialog()
    await pick()
    expect(await screen.findByTestId('move-refusal')).toHaveTextContent(PUBLIC_REPO_COPY)
    expect(screen.queryByRole('checkbox', { name: CARRY_DIRTY_COPY })).toBeNull()
    expect(moveButton()).toBeDisabled()
  })

  it('a clean tree never sends carryDirty', async () => {
    get.mockResolvedValue(preflight())
    post.mockResolvedValue({ sessionId: 's1', from: 'dobby', to: 'rocky', outcome: 'moved', finalized: true, recoveryRef: null, session: null })
    renderDialog()
    await pick()
    await waitFor(() => expect(moveButton()).toBeEnabled())
    await userEvent.click(moveButton())
    expect(post).toHaveBeenCalledWith('/sessions/s1/move', { host: 'rocky', carryDirty: false, stopBackgroundTasks: false })
  })

  it('renders an untrusted path as text, not markup', async () => {
    const evil = '<img src=x onerror=alert(1)>.ts'
    get.mockResolvedValue(preflight({ dirty: { count: 1, paths: [evil] }, repoPrivate: true }))
    renderDialog()
    await pick()
    const list = await screen.findByTestId('move-dirty-paths')
    expect(list).toHaveTextContent(evil)
    expect(list.querySelector('img')).toBeNull()
  })
})

describe('MoveSessionDialog — background tasks', () => {
  it('lists the tasks that stop and needs a confirm', async () => {
    get.mockResolvedValue(preflight({
      backgroundTasks: [{ kind: 'monitor', name: 'Monitor', description: 'tail ci', replayable: true }],
    }))
    post.mockResolvedValue({ sessionId: 's1', from: 'dobby', to: 'rocky', outcome: 'moved', finalized: true, recoveryRef: null, session: null })
    renderDialog()
    await pick()
    expect(await screen.findByTestId('move-background-tasks')).toHaveTextContent('Monitor — tail ci')
    expect(moveButton()).toBeDisabled()
    await userEvent.click(screen.getByRole('checkbox', { name: 'Stop these tasks and move' }))
    await userEvent.click(moveButton())
    expect(post).toHaveBeenCalledWith('/sessions/s1/move', { host: 'rocky', carryDirty: false, stopBackgroundTasks: true })
  })
})

describe('MoveSessionDialog — outcomes', () => {
  async function submitWith(result: () => Promise<unknown>) {
    get.mockResolvedValue(preflight())
    post.mockImplementation(result)
    const utils = renderDialog()
    await pick()
    await waitFor(() => expect(moveButton()).toBeEnabled())
    await userEvent.click(moveButton())
    return utils
  }

  it('moved: "Moved to <host>", the host re-attaches, sessions refetch', async () => {
    const spy = vi.spyOn(QueryClient.prototype, 'invalidateQueries')
    const { onSettled } = await submitWith(async () => ({
      sessionId: 's1', from: 'dobby', to: 'rocky', outcome: 'moved', finalized: true, recoveryRef: null, session: null,
    }))
    expect(await screen.findByTestId('move-outcome')).toHaveTextContent('Moved to rocky')
    expect(onSettled).toHaveBeenCalled()
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: ['sessions'] }))
    spy.mockRestore()
  })

  it('shows progress while the move runs', async () => {
    let resolve: (v: unknown) => void = () => {}
    await submitWith(() => new Promise((r) => { resolve = r }))
    expect(await screen.findByRole('status')).toHaveTextContent('Moving to rocky…')
    resolve({ sessionId: 's1', from: 'dobby', to: 'rocky', outcome: 'moved', finalized: true, recoveryRef: null, session: null })
    expect(await screen.findByTestId('move-outcome')).toHaveTextContent('Moved to rocky')
  })

  it('restored: back on the source, with the reason', async () => {
    const { onSettled } = await submitWith(async () => {
      throw new ApiRequestError(409, { detail: { code: 'MOVE_IMPORT_FAILED', message: 'fetch failed', outcome: 'restored' } })
    })
    const out = await screen.findByTestId('move-outcome')
    expect(out).toHaveTextContent('Move failed — the session is back on dobby')
    expect(out).toHaveTextContent(MOVE_REASON_COPY.MOVE_IMPORT_FAILED)
    expect(onSettled).toHaveBeenCalled()
  })

  it('failed: cleanup is still running', async () => {
    const { onSettled } = await submitWith(async () => {
      throw new ApiRequestError(409, { detail: { code: 'MOVE_RESUME_FAILED', message: 'x', outcome: 'failed' } })
    })
    expect(await screen.findByTestId('move-outcome')).toHaveTextContent('Move failed — finishing cleanup; this may take a minute')
    expect(onSettled).not.toHaveBeenCalled()
  })

  it('a refusal before the claim returns to the form with its reason', async () => {
    await submitWith(async () => {
      throw new ApiRequestError(409, { detail: { code: 'MOVE_MID_TURN', message: 'x' } })
    })
    expect(await screen.findByTestId('move-refusal')).toHaveTextContent('Wait for the turn to finish or Interrupt first')
    expect(screen.queryByTestId('move-outcome')).toBeNull()
  })
})
