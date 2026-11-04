/**
 * D-15: the session PR tab — header, grouped checks, review threads, and the merge
 * actions, each shown only where it applies. The data hooks are mocked; the markdown
 * renderer is the real (sanitized) one, because Review Focus 3 is about it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useEffect } from 'react'
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'
import type { PrDetail, PullRequestInfo, PrReviewThread } from '@/api/types'
import { ApiRequestError } from '@/api/client'
import { SessionGestureProvider, useComposerAttachRegistration } from '@/shell/sessionGestures'
import type { ComposerAttach } from '@/features/sessions/composer/Composer'
import PrTab from './PrTab'

const h = vi.hoisted(() => ({
  detail: vi.fn(),
  merge: vi.fn(),
  autoMerge: vi.fn(),
  fix: vi.fn(),
}))

vi.mock('@/api/hooks', () => ({
  usePrDetail: h.detail,
  useMergePr: () => ({ mutateAsync: h.merge, isPending: false }),
  useSetAutoMerge: () => ({ mutateAsync: h.autoMerge, isPending: false }),
  useFixPr: () => ({ mutateAsync: h.fix, isPending: false }),
}))

const PR: PullRequestInfo = {
  number: 42, url: 'https://github.com/acme/widgets/pull/42', state: 'open', checks: 'passing',
  title: 'Add the widget', deployed: false, repo: 'acme/widgets', is_draft: false,
  merge_state_status: 'CLEAN', auto_merge_enabled: false, in_merge_queue: false, labels: [],
}

const thread = (over: Partial<PrReviewThread>): PrReviewThread => ({
  id: 'T1', path: 'src/a.ts', line: 12, resolved: false, outdated: false,
  comments: [{ author: 'alice', body: 'Please rename this.', createdAt: '2026-11-01T00:00:00Z',
               url: 'https://github.com/acme/widgets/pull/42#discussion_r1' }],
  ...over,
})

function detail(over: Partial<PrDetail> = {}): PrDetail {
  return {
    ...PR, checks: [], reviews: [], threads: [], isDraft: false, autoMergeEnabled: false,
    mergeStateStatus: 'CLEAN', humanHold: false, ...over,
  }
}

const live = (d: PrDetail) => h.detail.mockReturnValue({ data: d, error: null, isError: false, isPending: false })

const draft: { current: ComposerAttach | null } = { current: null }
const setDraft = vi.fn()
let draftText = ''
function ComposerStub() {
  const ref = useComposerAttachRegistration()
  useEffect(() => {
    if (ref) (ref as { current: ComposerAttach | null }).current = {
      attachFile: vi.fn(), setDraft, draftText: () => draftText,
    }
    draft.current = ref?.current ?? null
  })
  return null
}

const onOpenFile = vi.fn()
const onShowConversation = vi.fn()
/** The same tree for render and rerender, so a rerender updates PrTab rather than remounting it. */
function tree(prs: PullRequestInfo[] = [PR], hasFiles = true) {
  return (
    <SessionGestureProvider>
      <ComposerStub />
      <PrTab sessionId="s1" prs={prs} hasFiles={hasFiles} onOpenFile={onOpenFile} onShowConversation={onShowConversation} />
    </SessionGestureProvider>
  )
}
function renderTab(prs: PullRequestInfo[] = [PR], hasFiles = true) {
  return render(tree(prs, hasFiles))
}

beforeEach(() => {
  vi.clearAllMocks()
  draftText = ''
  live(detail())
  h.merge.mockResolvedValue({ ok: true, error: '' })
  h.autoMerge.mockResolvedValue({ ok: true, error: '' })
  h.fix.mockResolvedValue({})
})
afterEach(() => { vi.restoreAllMocks() })

describe('PrTab — picker', () => {
  it('defaults to the lowest open/draft PR and switches on pick', () => {
    const prs = [
      { ...PR, number: 50, title: 'Later' },
      { ...PR, number: 41, title: 'Old merged', state: 'merged' as const },
      { ...PR, number: 45, title: 'Draft one', state: 'draft' as const },
    ]
    renderTab(prs)
    const picker = screen.getByRole('combobox', { name: 'Pull request' })
    expect(picker).toHaveDisplayValue('#45 Draft one')
    expect(h.detail).toHaveBeenLastCalledWith('acme/widgets', 45)
    fireEvent.change(picker, { target: { value: 'acme/widgets#50' } })
    expect(h.detail).toHaveBeenLastCalledWith('acme/widgets', 50)
  })

  it('has no picker for a single PR', () => {
    renderTab()
    expect(screen.queryByRole('combobox', { name: 'Pull request' })).toBeNull()
  })
})

describe('PrTab — header', () => {
  it('shows title, number, state, flags, the merge state in words and Open on GitHub', () => {
    live(detail({ autoMergeEnabled: true, in_merge_queue: true, humanHold: true, mergeStateStatus: 'BLOCKED' }))
    renderTab()
    const header = screen.getByRole('region', { name: 'Pull request #42' })
    expect(within(header).getByRole('heading', { name: 'Add the widget' })).toBeInTheDocument()
    expect(within(header).getByText('#42')).toBeInTheDocument()
    expect(within(header).getByText('Open')).toBeInTheDocument()
    expect(within(header).getByText('Auto-merge on')).toBeInTheDocument()
    expect(within(header).getByText('In merge queue')).toBeInTheDocument()
    expect(within(header).getByText('Held for a human')).toBeInTheDocument()
    expect(within(header).getByText('Blocked by required checks or reviews')).toBeInTheDocument()
    expect(within(header).getByRole('link', { name: /Open on GitHub/ })).toHaveAttribute('href', PR.url)
  })

  it.each([
    ['draft', true, 'Draft'], ['merged', false, 'Merged'], ['closed', false, 'Closed'],
  ] as const)('state %s reads %s', (state, isDraft, label) => {
    live(detail({ state, isDraft }))
    renderTab()
    expect(within(screen.getByRole('region', { name: 'Pull request #42' })).getByText(label)).toBeInTheDocument()
  })
})

describe('PrTab — checks', () => {
  it('groups failing / pending / passing with failing expanded', () => {
    live(detail({ checks: [
      { name: 'lint', status: 'COMPLETED', conclusion: 'success', url: 'https://ci/1' },
      { name: 'unit', status: 'COMPLETED', conclusion: 'failure', url: 'https://ci/2' },
      { name: 'e2e', status: 'IN_PROGRESS', conclusion: null, url: null },
    ] }))
    renderTab()
    const failing = screen.getByText('Failing (1)').closest('details')!
    const pending = screen.getByText('Pending (1)').closest('details')!
    const passing = screen.getByText('Passing (1)').closest('details')!
    expect(failing).toHaveAttribute('open')
    expect(pending).not.toHaveAttribute('open')
    expect(passing).not.toHaveAttribute('open')
    expect(within(failing).getByText('unit')).toBeInTheDocument()
    expect(within(failing).getByRole('link', { name: 'unit details' })).toHaveAttribute('href', 'https://ci/2')
  })
})

describe('PrTab — review threads', () => {
  it('lists unresolved first, collapses resolved under "N resolved", marks outdated', () => {
    live(detail({ threads: [
      thread({ id: 'R', resolved: true, path: 'src/old.ts', comments: [{ author: 'bob', body: 'done', createdAt: null, url: null }] }),
      thread({ id: 'U', outdated: true }),
    ] }))
    renderTab()
    const region = screen.getByRole('region', { name: 'Review threads' })
    const items = within(region).getAllByRole('article')
    expect(items[0]).toHaveTextContent('src/a.ts:12')
    expect(within(items[0]).getByText('Outdated')).toBeInTheDocument()
    const resolved = screen.getByText('1 resolved').closest('details')!
    expect(resolved).not.toHaveAttribute('open')
    expect(within(resolved).getByText('src/old.ts:12')).toBeInTheDocument()
  })

  it('links every path:line to the Files tab when the session has a worktree', () => {
    live(detail({ threads: [thread({}), thread({ id: 'T2', path: 'gone.ts', line: 3 })] }))
    renderTab()
    fireEvent.click(screen.getByRole('button', { name: 'src/a.ts:12' }))
    expect(onOpenFile).toHaveBeenCalledWith('src/a.ts', 12)
    // No per-path existence query: a missing file is the Files tab's FILE_NOT_FOUND.
    fireEvent.click(screen.getByRole('button', { name: 'gone.ts:3' }))
    expect(onOpenFile).toHaveBeenLastCalledWith('gone.ts', 3)
  })

  it('no Files link without a worktree', () => {
    live(detail({ threads: [thread({})] }))
    renderTab([PR], false)
    expect(screen.queryByRole('button', { name: 'src/a.ts:12' })).toBeNull()
  })

  it('"Ask session to address" prefills the composer and shows the conversation, without sending', () => {
    live(detail({ threads: [thread({})] }))
    renderTab()
    fireEvent.click(screen.getByRole('button', { name: 'Ask session to address' }))
    expect(setDraft).toHaveBeenCalledWith('Address this review comment on src/a.ts:12:\n\n> Please rename this.')
    expect(onShowConversation).toHaveBeenCalled()
    expect(h.merge).not.toHaveBeenCalled()
    expect(h.fix).not.toHaveBeenCalled()
  })

  it('asks before replacing an unsent draft', async () => {
    draftText = 'half-written'
    live(detail({ threads: [thread({})] }))
    renderTab()
    fireEvent.click(screen.getByRole('button', { name: 'Ask session to address' }))
    const dialog = await screen.findByRole('dialog')
    expect(setDraft).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Replace' }))
    await waitFor(() => expect(setDraft).toHaveBeenCalled())
    expect(onShowConversation).toHaveBeenCalled()
  })

  it('renders <script> and javascript: links inert (Review Focus 3)', async () => {
    live(detail({ threads: [thread({ comments: [{
      author: 'mallory',
      body: 'Look <script>window.__pwned = 1</script> and [click me](javascript:alert(1)) <img src=x onerror="window.__pwned=2">',
      createdAt: null, url: 'javascript:alert(2)',
    }] })] }))
    const { container } = renderTab()
    await screen.findByText('click me')
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('[onerror]')).toBeNull()
    for (const a of Array.from(container.querySelectorAll('a'))) {
      expect(a.getAttribute('href') ?? '').not.toMatch(/^\s*javascript:/i)
    }
    // The comment's own link is not rendered for a non-http(s) url.
    expect(screen.queryByRole('link', { name: /View comment on GitHub/ })).toBeNull()
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined()
  })

  it('never fetches a comment image: alt text plus an http(s) link, no <img> (privacy)', async () => {
    live(detail({ threads: [thread({ comments: [{
      author: 'eve', createdAt: null, url: null,
      body: 'Tracked ![pixel](https://evil.example/t.png) and <img src="https://evil.example/raw.png" alt="raw"> and ![bad](javascript:alert(1))',
    }] })] }))
    const { container } = renderTab()
    const link = await screen.findByRole('link', { name: '[pixel]' })
    expect(link).toHaveAttribute('href', 'https://evil.example/t.png')
    expect(container.querySelector('img')).toBeNull()
    expect(screen.queryByRole('link', { name: '[bad]' })).toBeNull()
    expect(screen.getByText('[bad]')).toBeInTheDocument()
  })

  it('keeps only absolute http(s) links clickable in a comment', async () => {
    live(detail({ threads: [thread({ comments: [{
      author: 'eve', createdAt: null, url: null,
      body: '[api](/api/sessions/s1/archive) [rel](../x) [anchor](#top) [ok](https://example.com/doc)',
    }] })] }))
    renderTab()
    expect(await screen.findByRole('link', { name: 'ok' })).toHaveAttribute('href', 'https://example.com/doc')
    for (const name of ['api', 'rel', 'anchor']) {
      expect(screen.queryByRole('link', { name })).toBeNull()
      expect(screen.getByText(name)).toBeInTheDocument()
    }
  })

  it('links a comment with an https url', () => {
    live(detail({ threads: [thread({})] }))
    renderTab()
    expect(screen.getByRole('link', { name: /View comment on GitHub/ }))
      .toHaveAttribute('href', 'https://github.com/acme/widgets/pull/42#discussion_r1')
  })
})

const actions = () => screen.getByRole('group', { name: 'Pull request actions' })
const actionNames = () => within(actions()).queryAllByRole('button').map((b) => b.textContent)

describe('PrTab — actions', () => {
  it('open PR, CLEAN, no auto-merge: Ready + auto-merge and Merge now', () => {
    renderTab()
    expect(actionNames()).toEqual(['Ready + auto-merge', 'Merge now'])
  })

  it('draft: Ready + auto-merge only (no Merge now)', () => {
    live(detail({ state: 'draft', isDraft: true, mergeStateStatus: 'DRAFT' }))
    renderTab([{ ...PR, state: 'draft' }])
    expect(actionNames()).toEqual(['Ready + auto-merge'])
  })

  it('auto-merge on: Disable auto-merge, no arm', () => {
    live(detail({ autoMergeEnabled: true, mergeStateStatus: 'BLOCKED' }))
    renderTab()
    expect(actionNames()).toEqual(['Disable auto-merge'])
  })

  it('failing checks: Fix, through useFixPr', async () => {
    live(detail({ mergeStateStatus: 'BLOCKED' }))
    renderTab([{ ...PR, checks: 'failing' }])
    expect(actionNames()).toContain('Fix')
    fireEvent.click(within(actions()).getByRole('button', { name: 'Fix' }))
    await waitFor(() => expect(h.fix).toHaveBeenCalledWith({ id: 's1', number: 42 }))
  })

  it('Ready + auto-merge is blocked (with the reason) on a conflicting PR', () => {
    live(detail({ mergeStateStatus: 'DIRTY' }))
    renderTab()
    expect(within(actions()).getByRole('button', { name: 'Ready + auto-merge' })).toBeDisabled()
    expect(screen.getByText(/conflicts with base/)).toBeInTheDocument()
  })

  it('arms with enabled: true and reports the result', async () => {
    h.autoMerge.mockResolvedValue({ ok: true, error: '', markedReady: false, autoMergeArmed: true, alreadyArmed: false, cleanReadyToMerge: false })
    renderTab()
    fireEvent.click(within(actions()).getByRole('button', { name: 'Ready + auto-merge' }))
    await waitFor(() => expect(h.autoMerge).toHaveBeenCalledWith({ repo: 'acme/widgets', number: 42, enabled: true }))
    expect(await screen.findByText('Auto-merge armed')).toBeInTheDocument()
  })

  it('disables with enabled: false and shows gh\'s text on failure', async () => {
    live(detail({ autoMergeEnabled: true }))
    h.autoMerge.mockResolvedValue({ ok: false, error: 'GraphQL: auto merge is not allowed' })
    renderTab()
    fireEvent.click(within(actions()).getByRole('button', { name: 'Disable auto-merge' }))
    await waitFor(() => expect(h.autoMerge).toHaveBeenCalledWith({ repo: 'acme/widgets', number: 42, enabled: false }))
    expect(await screen.findByText(/GraphQL: auto merge is not allowed/)).toBeInTheDocument()
  })

  it('Merge now asks first, then merges', async () => {
    renderTab()
    fireEvent.click(within(actions()).getByRole('button', { name: 'Merge now' }))
    const dialog = await screen.findByRole('dialog')
    expect(h.merge).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Merge' }))
    await waitFor(() => expect(h.merge).toHaveBeenCalledWith({ repo: 'acme/widgets', number: 42 }))
    expect(await screen.findByText('Merged')).toBeInTheDocument()
  })

  it('Merge now cancelled does nothing', async () => {
    renderTab()
    fireEvent.click(within(actions()).getByRole('button', { name: 'Merge now' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(h.merge).not.toHaveBeenCalled()
  })

  it('maps a 409 PR_HUMAN_HOLD refusal to the hold copy', async () => {
    h.merge.mockRejectedValue(new ApiRequestError(409, { detail: { code: 'PR_HUMAN_HOLD', error: 'acme/widgets#42 carries hold' } }))
    renderTab()
    fireEvent.click(within(actions()).getByRole('button', { name: 'Merge now' }))
    fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Merge' }))
    expect(await screen.findByText('Held for a human — merge it on GitHub')).toBeInTheDocument()
  })

  it('maps a 409 PR_HUMAN_HOLD_UNKNOWN refusal to the retry copy', async () => {
    h.autoMerge.mockRejectedValue(new ApiRequestError(409, { detail: { code: 'PR_HUMAN_HOLD_UNKNOWN', error: 'gh: timeout' } }))
    renderTab()
    fireEvent.click(within(actions()).getByRole('button', { name: 'Ready + auto-merge' }))
    expect(await screen.findByText('Couldn\'t confirm the hold state — try again')).toBeInTheDocument()
  })

  it('a hold hides merge, arm and Fix but keeps Disable auto-merge', () => {
    live(detail({ humanHold: true, autoMergeEnabled: true }))
    renderTab([{ ...PR, checks: 'failing' }])
    expect(actionNames()).toEqual(['Disable auto-merge'])
    expect(within(actions()).getByText('Held for a human — merge it on GitHub')).toBeInTheDocument()
  })

  it('a PR merged while the tab is open drops every action but Open on GitHub (Review Focus 5)', () => {
    live(detail({ autoMergeEnabled: true }))
    const view = renderTab([{ ...PR, checks: 'failing' }])
    expect(actionNames().length).toBeGreaterThan(0)
    live(detail({ state: 'merged', merged: true, autoMergeEnabled: false, mergeStateStatus: 'UNKNOWN' }))
    view.rerender(tree([{ ...PR, checks: 'failing' }]))
    expect(within(screen.getByRole('region', { name: 'Pull request #42' })).getByText('Merged')).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Pull request actions' })).toBeNull()
    expect(screen.getByRole('link', { name: /Open on GitHub/ })).toBeInTheDocument()
  })
})

describe('PrTab — the pick is latched (Review Focus 5)', () => {
  it('with two open PRs, merging the shown one keeps the tab on it, Merged, with only Open on GitHub', () => {
    let merged = false
    h.detail.mockImplementation((_repo: string, n: number) => ({
      isPending: false, isError: false, error: null,
      data: detail({ number: n, title: `PR ${n}`, url: `https://github.com/acme/widgets/pull/${n}`,
                     ...(n === 10 && merged ? { state: 'merged', merged: true, mergeStateStatus: 'UNKNOWN' } : {}) }),
    }))
    const open = (n: number): PullRequestInfo => ({ ...PR, number: n, title: `PR ${n}`, url: `https://github.com/acme/widgets/pull/${n}` })
    const view = renderTab([open(10), open(12)])
    expect(screen.getByRole('combobox', { name: 'Pull request' })).toHaveDisplayValue('#10 PR 10')
    // The poll (and the sessions refetch) report #10 merged.
    merged = true
    view.rerender(tree([{ ...open(10), state: 'merged' }, open(12)]))
    expect(screen.getByRole('combobox', { name: 'Pull request' })).toHaveDisplayValue('#10 PR 10')
    const header = screen.getByRole('region', { name: 'Pull request #10' })
    expect(within(header).getByText('Merged')).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Pull request actions' })).toBeNull()
    expect(within(header).getByRole('link', { name: /Open on GitHub/ })).toBeInTheDocument()
  })

  it('latches the default when the PR list arrives after mount', () => {
    const view = renderTab([])
    expect(screen.queryByRole('region', { name: /Pull request #/ })).toBeNull()
    view.rerender(tree([{ ...PR, number: 42 }, { ...PR, number: 50 }]))
    expect(screen.getByRole('combobox', { name: 'Pull request' })).toHaveDisplayValue('#42 Add the widget')
  })
})

describe('PrTab — detail failure', () => {
  it('falls back to the payload: title, state and checks, with the reason', () => {
    h.detail.mockReturnValue({
      data: undefined, isPending: false, isError: true,
      error: new ApiRequestError(502, { detail: 'PR sync for acme/widgets is failing: rate limited' }),
    })
    renderTab([{ ...PR, checks: 'failing', check_groups: [{ kind: 'head', total_seconds: 0, cost_usd: null, checks: [
      { name: 'unit', workflow: null, status: 'COMPLETED', conclusion: 'failure', duration_seconds: 1, required: true, overdue: false },
    ] }] }])
    expect(screen.getByText('Live details unavailable — PR sync for acme/widgets is failing: rate limited')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Add the widget' })).toBeInTheDocument()
    expect(screen.getByText('Failing (1)')).toBeInTheDocument()
    expect(screen.getByText('unit')).toBeInTheDocument()
  })
})
