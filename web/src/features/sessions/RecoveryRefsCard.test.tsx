/**
 * RecoveryRefsCard (D-21): an archived session's recovery snapshots. Hidden while
 * loading, on any error and with no refs; otherwise each ref's reason, short SHA and
 * date, and the restore command as literal text that copies exactly.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import * as hooks from '@/api/hooks'
import { copyText } from '@/lib/copyText'
import { queryResult } from '@/test/reactQuery'
import RecoveryRefsCard from './RecoveryRefsCard'

vi.mock('@/api/hooks', () => ({ useSessionRecoveryRefs: vi.fn() }))
vi.mock('@/lib/copyText', () => ({ copyText: vi.fn(async () => true) }))

const REFS = {
  refs: [
    { ref: 'refs/viberpit/recovery/abcd1234/discard-1', reason: 'discard', sha: '0123456789abcdef0123', createdAt: '2026-11-05T14:30:00Z' },
    { ref: 'refs/viberpit/recovery/abcd1234/close-1', reason: null, sha: 'fedcba9876543210fedc', createdAt: null },
  ],
  restoreCommand: 'git -C /srv/clone worktree add ../restored refs/viberpit/recovery/abcd1234/discard-1',
}

const respond = (value: unknown) => vi.mocked(hooks.useSessionRecoveryRefs).mockReturnValue(value as never)
const card = () => screen.queryByRole('region', { name: 'Recovery snapshots' })
/** The restore command's copy control (`CopyablePath`). */
const restoreControl = () => screen.getByTitle('Copy restore command')

describe('RecoveryRefsCard', () => {
  beforeEach(() => vi.clearAllMocks())

  it('asks for the session it is given', () => {
    respond({ data: undefined })
    render(<RecoveryRefsCard sessionId="arc-99" />)
    expect(hooks.useSessionRecoveryRefs).toHaveBeenCalledWith('arc-99')
  })

  it.each([
    ['loading', { data: undefined, isLoading: true }],
    ['an error (404 / 409 daemon_outdated)', { data: undefined, isError: true, error: new Error('409') }],
    ['no refs', queryResult({ refs: [], restoreCommand: null })],
    ['a body with no refs array (a catch-all answer)', queryResult([])],
  ])('renders nothing while %s', (_label, value) => {
    respond(value)
    const { container } = render(<RecoveryRefsCard sessionId="arc-99" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('lists each ref with its reason, short SHA and date when known', () => {
    respond(queryResult(REFS))
    render(<RecoveryRefsCard sessionId="arc-99" />)
    const items = screen.getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('discard')
    expect(items[0]).toHaveTextContent('0123456')
    expect(items[0]).not.toHaveTextContent('01234567')
    expect(items[0]).toHaveTextContent(new Date(REFS.refs[0].createdAt!).toLocaleString())
    expect(items[0]).toHaveAttribute('title', REFS.refs[0].ref)
    expect(items[1]).toHaveTextContent('Snapshot')
    expect(items[1]).toHaveTextContent('fedcba9')
  })

  it('copies the restore command exactly, by click or Enter', async () => {
    const user = userEvent.setup()
    respond(queryResult(REFS))
    render(<RecoveryRefsCard sessionId="arc-99" />)
    expect(restoreControl()).toHaveRole('button')
    expect(restoreControl().textContent).toBe(REFS.restoreCommand)
    await user.click(restoreControl())
    expect(copyText).toHaveBeenCalledWith(REFS.restoreCommand)
    expect(await screen.findByTitle('Copied!')).toBeInTheDocument()
    vi.mocked(copyText).mockClear()
    screen.getByTitle('Copied!').focus()
    await user.keyboard('{Enter}')
    expect(copyText).toHaveBeenCalledWith(REFS.restoreCommand)
  })

  it('renders the restore command and reasons as text, never markup', async () => {
    const user = userEvent.setup()
    const hostile = 'git restore "<img src=x onerror=alert(1)>" && echo $(whoami) <b>bold</b>'
    respond(queryResult({
      refs: [{ ref: 'refs/viberpit/recovery/x/<i>r</i>', reason: '<script>bad()</script>', sha: 'aaaaaaaaaa', createdAt: null }],
      restoreCommand: hostile,
    }))
    const { container } = render(<RecoveryRefsCard sessionId="arc-99" />)
    expect(restoreControl().textContent).toBe(hostile)
    expect(screen.getByRole('listitem')).toHaveTextContent('<script>bad()</script>')
    expect(container.querySelector('img, b, script, i')).toBeNull()
    await user.click(restoreControl())
    expect(copyText).toHaveBeenCalledWith(hostile)
  })

  it('without a restore command it lists the refs and offers no copy', () => {
    respond(queryResult({ ...REFS, restoreCommand: null }))
    render(<RecoveryRefsCard sessionId="arc-99" />)
    expect(card()).toBeInTheDocument()
    expect(screen.queryByTitle('Copy restore command')).toBeNull()
  })
})
