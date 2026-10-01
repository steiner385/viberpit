import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import * as hooks from '@/api/hooks'
import * as controller from '@/lib/deployController'
import type { PendingDeploy, DeployProgress } from '@/api/types'
import { DeploySection } from './DeploySection'

expect.extend(toHaveNoViolations)

function mockHooks(
  pending: Partial<PendingDeploy>,
  deploy: Partial<DeployProgress>,
  start = vi.fn(),
) {
  vi.spyOn(hooks, 'usePendingDeploy').mockReturnValue(
    { data: pending } as unknown as ReturnType<typeof hooks.usePendingDeploy>)
  vi.spyOn(hooks, 'useDeployStatus').mockReturnValue(
    { data: deploy } as unknown as ReturnType<typeof hooks.useDeployStatus>)
  vi.spyOn(controller, 'useDeployController').mockReturnValue({
    intent: 'idle', visible: false, starting: false, startError: null,
    start, dismiss: vi.fn(),
  })
  return start
}

const COMMITS = [
  { sha: 'aaaaaaa1111', subject: 'fix the thing', author: 'tony', iso: '2026-09-06T00:00:00Z' },
  { sha: 'bbbbbbb2222', subject: 'add the other thing', author: 'tony', iso: '2026-09-06T01:00:00Z' },
]

describe('DeploySection', () => {
  beforeEach(() => vi.restoreAllMocks())

  // This section is why the banner may be hidden on phone at all. If it went
  // quiet when there was nothing to do, a phone user would have no way to tell
  // "up to date" from "this page is broken".
  it('says the server is up to date when nothing is pending', () => {
    mockHooks({ behind: false, commits: [] }, { running: false })
    render(<DeploySection />)
    expect(screen.getByRole('heading', { level: 2, name: 'Deploy' })).toBeInTheDocument()
    expect(screen.getByText(/up to date/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Deploy now/ })).not.toBeInTheDocument()
  })

  it('lists every pending commit and enables Deploy when CI is green', () => {
    const start = mockHooks(
      { behind: true, commits: COMMITS, ci: 'green' }, { running: false })
    render(<DeploySection />)
    expect(screen.getByText('fix the thing')).toBeInTheDocument()
    expect(screen.getByText('add the other thing')).toBeInTheDocument()
    const button = screen.getByRole('button', { name: /Deploy now/ })
    expect(button).toBeEnabled()
    fireEvent.click(button)
    // undefined = deploy the tip, the same argument the banner sends.
    expect(start).toHaveBeenCalledWith(undefined)
  })

  // The green-frontier rule is the subtlest thing this section shares with the
  // banner: the tip is red, but an older pending commit is green and IS
  // deployable. Two surfaces disagreeing here means one offers a deploy the
  // other calls impossible.
  it('offers the green frontier and says how much it covers when the tip is red', () => {
    const start = mockHooks({
      behind: true, commits: COMMITS, ci: 'green', tipCi: 'red',
      deployableSha: 'aaaaaaa1111', deployableCount: 1,
    }, { running: false })
    render(<DeploySection />)
    expect(screen.getByText(/1 of 2 deployable/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Deploy now/ }))
    expect(start).toHaveBeenCalledWith('aaaaaaa1111')
  })

  it('translates an unrun verdict rather than calling it pending', () => {
    mockHooks({ behind: true, commits: COMMITS, ci: 'unrun' }, { running: false })
    render(<DeploySection />)
    expect(screen.getByText(/never ran/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Deploy now/ })).toBeDisabled()
  })

  it('disables Deploy while one is already running', () => {
    mockHooks({ behind: true, commits: COMMITS, ci: 'green' }, { running: true })
    render(<DeploySection />)
    expect(screen.getByRole('button', { name: /Deploy now/ })).toBeDisabled()
    expect(screen.getByText(/deploying/)).toBeInTheDocument()
  })

  // The banner lets you dismiss this; the section must NOT, because it is the
  // place you come to after dismissing it — or, on a phone, the only place it
  // was ever shown.
  it('always shows gave-up sessions, with no dismiss control', () => {
    mockHooks({ behind: false, commits: [] }, {
      running: false, restore: { failed: [], gaveUp: ['sess-42'] },
    })
    render(<DeploySection />)
    expect(screen.getByText(/couldn’t be recovered/)).toBeInTheDocument()
    expect(screen.getByText(/sess-42/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /dismiss/i })).not.toBeInTheDocument()
  })

  // 2026-09-30 Settings critique: the description promises "how the last
  // deploy went" but only a failure was ever rendered.
  it('says a clean last deploy completed, with the sessions it restored and the running sha', () => {
    mockHooks({ behind: false, commits: [], liveSha: 'abcdef0123' }, {
      running: false,
      phases: Object.fromEntries(['preflight', 'save', 'update', 'report', 'runners', 'restart', 'resume'].map((k) => [k, 'done'])),
      restore: { restored: ['a', 'b', 'c'], failed: [] },
    })
    render(<DeploySection />)
    expect(screen.getByRole('group', { name: 'Last deploy' })).toBeInTheDocument()
    expect(screen.getByTestId('deploy-last-success')).toHaveTextContent('Completed — 3 sessions restored. Running abcdef0.')
  })

  it('says nothing about the last deploy when none has run, and yields to a failure', () => {
    mockHooks({ behind: false, commits: [] }, { running: false, phases: {} })
    const { unmount } = render(<DeploySection />)
    expect(screen.queryByTestId('deploy-last-success')).toBeNull()
    unmount()
    mockHooks({ behind: false, commits: [] }, {
      running: false, phases: { restart: 'done' }, restore: { failed: [], gaveUp: ['sess-42'] },
    })
    render(<DeploySection />)
    expect(screen.queryByTestId('deploy-last-success')).toBeNull()
    expect(screen.getByText(/sess-42/)).toBeInTheDocument()
  })

  // P0 of the same critique: a long commit subject widened the fieldset past a
  // phone viewport and pushed Deploy now off-canvas (x=676 in 390px). The
  // truncating chain has to be min-w-0 all the way down.
  it('keeps the commit list shrinkable so a long subject cannot push Deploy off-screen', () => {
    mockHooks({ behind: true, ci: 'green', commits: [
      { sha: 'ccccccc3333', subject: 'a'.repeat(200), author: 'tony', iso: '2026-09-06T02:00:00Z' },
    ] }, { running: false })
    render(<DeploySection />)
    const subject = screen.getByText('a'.repeat(200))
    expect(subject).toHaveClass('min-w-0', 'truncate')
    expect(subject.closest('li')).toHaveClass('min-w-0')
    expect(screen.getByRole('group', { name: 'Pending changes' })).toHaveClass('min-w-0')
  })

  it('has no accessibility violations', async () => {
    mockHooks({ behind: true, commits: COMMITS, ci: 'green' }, { running: false })
    const { container } = render(<DeploySection />)
    expect(await axe(container)).toHaveNoViolations()
  })
})
