/**
 * RecoveryRefsCard (D-21): an archived session's recovery snapshots. Hidden while
 * loading, on any error and with no refs; otherwise each ref's reason, short SHA,
 * date and its own restore command, as literal text that copies exactly — or, with
 * no command, its full ref name as visible text.
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

const CMD = (ref: string) => `git -C /srv/clone worktree add ../restored ${ref}`
const NEWEST = 'refs/viberpit/recovery/abcd1234/20261105T143000Z-discard'
const OLDER = 'refs/viberpit/recovery/abcd1234/20261101T090000Z-close'
const REFS = {
  refs: [
    { ref: NEWEST, reason: 'discard', sha: '0123456789abcdef0123', createdAt: '2026-11-05T14:30:00Z', restoreCommand: CMD(NEWEST) },
    { ref: OLDER, reason: 'close', sha: 'fedcba9876543210fedc', createdAt: '2026-11-01T09:00:00Z', restoreCommand: CMD(OLDER) },
  ],
  restoreCommand: CMD(NEWEST),
}

const respond = (value: unknown) => vi.mocked(hooks.useSessionRecoveryRefs).mockReturnValue(value as never)
const card = () => screen.queryByRole('region', { name: 'Recovery snapshots' })
/** Each ref's restore-command copy control (`CopyablePath`), in list order. */
const restoreControls = () => screen.getAllByTitle('Copy restore command')

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

  it('lists each ref with its reason, short SHA and date', () => {
    respond(queryResult(REFS))
    render(<RecoveryRefsCard sessionId="arc-99" />)
    const items = screen.getAllByRole('listitem')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('discard')
    expect(items[0]).toHaveTextContent('0123456')
    expect(items[0]).not.toHaveTextContent('01234567')
    expect(items[0]).toHaveTextContent(new Date(REFS.refs[0].createdAt).toLocaleString())
    expect(items[1]).toHaveTextContent('close')
    expect(items[1]).toHaveTextContent('fedcba9')
    expect(items[1]).toHaveTextContent(new Date(REFS.refs[1].createdAt).toLocaleString())
  })

  it('every ref — the older one too — copies its OWN restore command exactly, by click or Enter', async () => {
    const user = userEvent.setup()
    respond(queryResult(REFS))
    render(<RecoveryRefsCard sessionId="arc-99" />)
    const [newest, older] = restoreControls()
    expect(newest.textContent).toBe(CMD(NEWEST))
    expect(older.textContent).toBe(CMD(OLDER))
    expect(older).toHaveRole('button')
    await user.click(older)
    expect(copyText).toHaveBeenCalledWith(CMD(OLDER))
    // The copy is announced, not only shown in a tooltip.
    expect(await screen.findByText('Copied')).toHaveAttribute('aria-live', 'polite')
    vi.mocked(copyText).mockClear()
    newest.focus()
    await user.keyboard('{Enter}')
    expect(copyText).toHaveBeenCalledWith(CMD(NEWEST))
  })

  it('a ref with no restore command shows its full name as text and offers no copy', () => {
    respond(queryResult({
      refs: [{ ...REFS.refs[0] }, { ...REFS.refs[1], restoreCommand: undefined }],
      restoreCommand: CMD(NEWEST),
    }))
    render(<RecoveryRefsCard sessionId="arc-99" />)
    expect(card()).toBeInTheDocument()
    expect(restoreControls()).toHaveLength(1)
    expect(screen.getAllByRole('listitem')[1]).toHaveTextContent(OLDER)
  })

  it('the hint does not assume a pointer', () => {
    respond(queryResult(REFS))
    render(<RecoveryRefsCard sessionId="arc-99" />)
    expect(card()).not.toHaveTextContent(/click/i)
  })

  it('renders commands, refs and reasons as text, never markup', async () => {
    const user = userEvent.setup()
    const hostile = 'git restore "<img src=x onerror=alert(1)>" && echo $(whoami) <b>bold</b>'
    respond(queryResult({
      refs: [
        { ref: 'refs/viberpit/recovery/x/<i>r</i>', reason: '<script>bad()</script>', sha: 'aaaaaaaaaa',
          createdAt: '2026-11-05T14:30:00Z', restoreCommand: hostile },
        { ref: 'refs/viberpit/recovery/x/<u>u</u>', reason: 'close', sha: 'bbbbbbbbbb', createdAt: '2026-11-05T14:30:00Z' },
      ],
      restoreCommand: hostile,
    }))
    const { container } = render(<RecoveryRefsCard sessionId="arc-99" />)
    expect(restoreControls()[0].textContent).toBe(hostile)
    expect(screen.getAllByRole('listitem')[0]).toHaveTextContent('<script>bad()</script>')
    expect(screen.getAllByRole('listitem')[1]).toHaveTextContent('refs/viberpit/recovery/x/<u>u</u>')
    expect(container.querySelector('img, b, script, i, u')).toBeNull()
    await user.click(restoreControls()[0])
    expect(copyText).toHaveBeenCalledWith(hostile)
  })
})
