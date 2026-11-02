/**
 * D-5: the Changes tab's review draft — line comments saved per session, the
 * review bar (count, Discard all, Send to Claude), and one composed prompt sent
 * through `onSendReview`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ViewportProvider } from '@/shell/useViewport'
import { stubMatchMedia, WIDTH_TIER_QUERIES } from '@/test/matchMedia'
import { queryResult } from '@/test/reactQuery'
import { storageKey } from '@/lib/storageKey'
import type { LineComment } from '../review/reviewComments'
import type { FileDiff, SessionDiff } from './types'

vi.mock('@/api/hooks', () => ({ useSessionDiff: vi.fn() }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

import { useSessionDiff } from '@/api/hooks'
import { toast } from 'sonner'
import ChangesTab from './ChangesTab'

const FILE: FileDiff = {
  path: 'src/app.ts', oldPath: null, status: 'modified', additions: 2, deletions: 1, binary: false, tooLarge: false,
  hunks: [{ oldStart: 1, oldLines: 3, newStart: 1, newLines: 4, header: '', lines: [
    { kind: 'context', oldNo: 1, newNo: 1, text: 'a' },
    { kind: 'del', oldNo: 2, newNo: null, text: 'b' },
    { kind: 'add', oldNo: null, newNo: 2, text: 'B' },
    { kind: 'add', oldNo: null, newNo: 3, text: 'C' },
    { kind: 'context', oldNo: 3, newNo: 4, text: 'd' },
  ] }],
}

const DIFF: SessionDiff = { base: 'abc1234', head: 'def', files: [FILE], additions: 2, deletions: 1, truncated: false }

const DRAFT_KEY = storageKey('reviewDraft:s1')

const SEEDED: LineComment[] = [
  { id: 'c2', anchor: { path: 'src/app.ts', side: 'old', start: 2, end: 2 }, quote: ['b'], body: ' why remove? ' },
  { id: 'c1', anchor: { path: 'src/app.ts', side: 'new', start: 2, end: 3 }, quote: ['B', 'C'], body: 'rename' },
]

const EXPECTED_PROMPT = [
  'Review comments on the current changes (2):',
  '',
  '1. `src/app.ts` line 2–3:',
  '   > B',
  '   > C',
  '   rename',
  '',
  '2. `src/app.ts` line 2 (removed):',
  '   > b',
  '   why remove?',
  '',
  'Address each comment, then summarize what you changed.',
].join('\n')

function serve(d: SessionDiff) {
  vi.mocked(useSessionDiff).mockImplementation(() => queryResult(d))
}

function seed(comments: LineComment[]) {
  sessionStorage.setItem(DRAFT_KEY, JSON.stringify(comments))
}

function renderTab(onSendReview = vi.fn()) {
  stubMatchMedia(WIDTH_TIER_QUERIES.phone)
  const utils = render(
    <ViewportProvider kiosk={null}>
      <ChangesTab sessionId="s1" onSendReview={onSendReview} />
    </ViewportProvider>,
  )
  return { ...utils, onSendReview }
}

beforeEach(() => {
  sessionStorage.clear()
  vi.mocked(toast.success).mockReset()
  vi.mocked(useSessionDiff).mockReset()
  serve(DIFF)
  Element.prototype.scrollIntoView = vi.fn()
})

describe('ChangesTab review bar', () => {
  it('is hidden with no comments', () => {
    renderTab()
    expect(screen.queryByRole('button', { name: 'Send to Claude' })).not.toBeInTheDocument()
  })

  it('a comment added in the diff shows the bar with its count and persists the draft', async () => {
    const user = userEvent.setup()
    renderTab()
    await user.click(await screen.findByRole('button', { name: 'Comment on line 3' }))
    await user.type(await screen.findByRole('textbox', { name: /comment/i }), 'tidy{Control>}{Enter}{/Control}')
    const bar = screen.getByRole('region', { name: 'Review comments' })
    expect(bar).toHaveTextContent('1 comment')
    const saved = JSON.parse(sessionStorage.getItem(DRAFT_KEY) ?? '[]') as LineComment[]
    expect(saved).toHaveLength(1)
    expect(saved[0]).toMatchObject({ anchor: { path: 'src/app.ts', side: 'new', start: 3, end: 3 }, quote: ['C'], body: 'tidy' })
  })

  it('Send calls onSendReview with the exact composed prompt, then the draft is empty', async () => {
    const user = userEvent.setup()
    seed(SEEDED)
    const { onSendReview } = renderTab()
    const bar = screen.getByRole('region', { name: 'Review comments' })
    expect(bar).toHaveTextContent('2 comments')
    await user.click(within(bar).getByRole('button', { name: 'Send to Claude' }))
    expect(onSendReview).toHaveBeenCalledTimes(1)
    expect(onSendReview).toHaveBeenCalledWith(EXPECTED_PROMPT, [])
    expect(screen.queryByRole('region', { name: 'Review comments' })).not.toBeInTheDocument()
    expect(sessionStorage.getItem(DRAFT_KEY)).toBeNull()
    expect(toast.success).toHaveBeenCalledWith('Sent 2 comments')
  })

  it('Discard all asks first', async () => {
    const user = userEvent.setup()
    seed(SEEDED)
    renderTab()
    await user.click(screen.getByRole('button', { name: 'Discard all' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Discard 2 comments?')
    await user.click(screen.getByRole('button', { name: 'Keep' }))
    expect(screen.getByRole('region', { name: 'Review comments' })).toHaveTextContent('2 comments')
    expect(sessionStorage.getItem(DRAFT_KEY)).not.toBeNull()
    await user.click(screen.getByRole('button', { name: 'Discard all' }))
    await user.click(screen.getByRole('button', { name: 'Discard' }))
    expect(screen.queryByRole('region', { name: 'Review comments' })).not.toBeInTheDocument()
    expect(sessionStorage.getItem(DRAFT_KEY)).toBeNull()
  })

  it('a refetched diff where the line moved shows the comment as stale, and it is still sent', async () => {
    const user = userEvent.setup()
    seed([SEEDED[1]])
    serve({ ...DIFF, files: [{ ...FILE, hunks: [{ ...FILE.hunks[0], lines: [
      { kind: 'context', oldNo: 1, newNo: 1, text: 'a' },
      { kind: 'add', oldNo: null, newNo: 2, text: 'inserted' },
      { kind: 'del', oldNo: 2, newNo: null, text: 'b' },
      { kind: 'add', oldNo: null, newNo: 3, text: 'B' },
      { kind: 'add', oldNo: null, newNo: 4, text: 'C' },
      { kind: 'context', oldNo: 3, newNo: 5, text: 'd' },
    ] }] }] })
    const { onSendReview } = renderTab()
    const card = await screen.findByRole('article', { name: /comment on lines? 2/i })
    expect(card).toHaveTextContent(/outdated/i)
    await user.click(screen.getByRole('button', { name: 'Send to Claude' }))
    expect(onSendReview.mock.calls[0][0]).toContain('1. `src/app.ts` line 2–3:\n   > B\n   > C\n   rename')
  })

  it('a comment on a file no longer in the diff is still shown and sent', async () => {
    seed([{ ...SEEDED[1], anchor: { ...SEEDED[1].anchor, path: 'src/gone.ts' } }])
    renderTab()
    const card = await screen.findByRole('article', { name: /comment on lines? 2/i })
    expect(card).toHaveTextContent('src/gone.ts')
    expect(screen.getByRole('region', { name: 'Review comments' })).toHaveTextContent('1 comment')
  })
})
