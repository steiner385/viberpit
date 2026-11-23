// web/src/features/delivery/automation/actions.test.tsx
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, within, waitFor } from '@testing-library/react'
import { toast } from 'sonner'
import { AutomationActions } from './actions'
import { OUTCOME_TTL_MS } from './useActionJobs'

const { mutateAsync, fetchPrDoctorJob } = vi.hoisted(() => ({
  mutateAsync: vi.fn(), fetchPrDoctorJob: vi.fn(),
}))
let items: unknown[] = []
vi.mock('@/api/hooks', () => ({
  useAutomationActionable: () => ({ data: items }),
  useAutomationTrigger: () => ({ mutateAsync, isPending: false }),
}))
vi.mock('@/api/automations', async (orig) => ({
  ...(await orig<typeof import('@/api/automations')>()),
  fetchPrDoctorJob,
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() } }))

beforeEach(() => {
  mutateAsync.mockReset()
  fetchPrDoctorJob.mockReset()
  vi.mocked(toast.success).mockClear()
  vi.mocked(toast.warning).mockClear()
  vi.mocked(toast.error).mockClear()
})
afterEach(() => { vi.useRealTimers() })

describe('AutomationActions', () => {
  // #16348: PR Doctor's click path refuses "prior PR execution has not
  // stopped" while a predecessor session still holds the claim — a
  // status-only item (no `actions`) must render the reason as plain text,
  // never an inert button.
  it('renders status text instead of a button when actions is empty', () => {
    items = [{
      id: 'o/r#1', title: 'fix conflict', actions: [],
      status: 'Being worked by session sess-predecessor',
    }]
    render(<AutomationActions automationId="pr-doctor" enabled variant="full" />)
    expect(screen.getByText('Being worked by session sess-predecessor')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('renders the action button, not status text, once a claim is free', () => {
    items = [{
      id: 'o/r#1', title: 'fix conflict',
      actions: [{ kind: 'merge_conflict', label: 'Resolve conflicts', style: 'risky' }],
    }]
    render(<AutomationActions automationId="pr-doctor" enabled variant="full" />)
    expect(screen.getByRole('button', { name: 'Resolve conflicts' })).toBeInTheDocument()
  })

  // #2936: a paused automation's decisions stay listed and actionable, under a
  // heading that says why the Delivery badge left them out.
  it('paused: both variants keep the rows and their buttons, headed "Paused — N parked"', () => {
    items = [1, 2].map((n) => ({ id: `o/r#${n}`, title: `report ${n}`,
      actions: [{ kind: 'file_now', label: 'File now', style: 'safe' }] }))
    const { unmount } = render(<AutomationActions automationId="session-feedback" enabled variant="full" paused />)
    expect(screen.getByRole('heading', { name: 'Paused — 2 parked' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'File now' })).toHaveLength(2)
    unmount()
    render(<AutomationActions automationId="session-feedback" enabled variant="preview" paused />)
    expect(screen.getByRole('heading', { name: 'Paused — 2 parked' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Actionable items' })).toBeNull()
  })

  it('preview: three rows, a "See all N" button that hands off, and nothing at all when empty', () => {
    const onSeeAll = vi.fn()
    items = [1, 2, 3, 4, 5].map((n) => ({ id: `o/r#${n}`, title: `item ${n}`, actions: [] , status: 'waiting' }))
    render(<AutomationActions automationId="pr-doctor" enabled variant="preview" onSeeAll={onSeeAll} />)
    expect(screen.getByRole('heading', { name: 'Actionable items' })).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(3)
    expect(screen.queryByText('item 4')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'See all 5' }))
    expect(onSeeAll).toHaveBeenCalledTimes(1)
  })

  it('preview: no "See all" button at all when no onSeeAll is given, even past PREVIEW_ROWS (F6)', () => {
    items = [1, 2, 3, 4, 5].map((n) => ({ id: `o/r#${n}`, title: `item ${n}`, actions: [], status: 'waiting' }))
    render(<AutomationActions automationId="pr-doctor" enabled variant="preview" />)
    expect(screen.queryByRole('button', { name: /See all/ })).toBeNull()
  })

  it('preview: no "See all" when the list fits, and null when it is empty', () => {
    items = [{ id: 'o/r#1', title: 'only', actions: [], status: 'waiting' }]
    const { unmount } = render(<AutomationActions automationId="pr-doctor" enabled variant="preview" />)
    expect(screen.queryByRole('button', { name: /See all/ })).toBeNull()
    unmount()
    items = []
    const { container } = render(<AutomationActions automationId="pr-doctor" enabled variant="preview" />)
    expect(container).toBeEmptyDOMElement()
  })

  // 2026-10-01 Delivery critique, P0: a muted signature's Unmute is not a
  // decision. Maintenance rows are folded under the list in `full`, never
  // counted in the preview's "See all N", and the subtitle is finally shown.
  it('splits decisions from maintenance: folded in full, absent from the preview count', () => {
    items = [
      { id: 'fpA', title: 'decide me', subtitle: 'WARNING · 3 since filed', actions: [{ kind: 'file_now', label: 'File now' }] },
      ...[1, 2, 3, 4].map((n) => ({ id: `fp${n}`, title: `muted ${n}`, kind: 'maintenance', actions: [{ kind: 'unmute', label: 'Unmute' }] })),
    ]
    const { unmount } = render(<AutomationActions automationId="error-triage" enabled variant="full" />)
    expect(screen.getByText('decide me')).toBeInTheDocument()
    expect(screen.getByText('WARNING · 3 since filed')).toBeInTheDocument()
    const fold = screen.getByText('4 maintenance')
    expect(fold.closest('details')).not.toHaveAttribute('open')
    expect(screen.getAllByRole('button', { name: 'Unmute' })).toHaveLength(4)
    unmount()
    render(<AutomationActions automationId="error-triage" enabled variant="preview" onSeeAll={vi.fn()} />)
    expect(screen.getAllByRole('listitem')).toHaveLength(1)
    expect(screen.queryByRole('button', { name: /See all/ })).toBeNull()
    expect(screen.queryByText(/maintenance/)).toBeNull()
  })

  it('full: says nothing needs you while still offering the maintenance fold; preview is empty', () => {
    items = [{ id: 'fp1', title: 'muted 1', kind: 'maintenance', actions: [{ kind: 'unmute', label: 'Unmute' }] }]
    const { unmount } = render(<AutomationActions automationId="error-triage" enabled variant="full" />)
    expect(screen.getByText('Nothing needs you.')).toBeInTheDocument()
    expect(screen.getByText('1 maintenance')).toBeInTheDocument()
    unmount()
    const { container } = render(<AutomationActions automationId="error-triage" enabled variant="preview" />)
    expect(container).toBeEmptyDOMElement()
  })

  it('full: every row, no heading, no scroll box, and a sentence when empty', () => {
    items = [1, 2, 3, 4, 5].map((n) => ({ id: `o/r#${n}`, title: `item ${n}`, actions: [], status: 'waiting' }))
    const { unmount, container } = render(<AutomationActions automationId="pr-doctor" enabled variant="full" />)
    expect(screen.getAllByRole('listitem')).toHaveLength(5)
    expect(screen.queryByRole('heading')).toBeNull()
    expect(container.querySelector('.overflow-y-auto')).toBeNull()
    unmount()
    items = []
    render(<AutomationActions automationId="pr-doctor" enabled variant="full" />)
    expect(screen.getByText('Nothing needs you.')).toBeInTheDocument()
  })

  // #3429: a Resolve-conflicts click on cairnea/KinDash#20904 was shown
  // steiner385/keel#65's untrusted_author refusal. Each result now comes only
  // from its own job, lands on its own row, and the toast names the item.
  describe('action results belong to their own item (#3429)', () => {
    const KINDASH = 'cairnea/KinDash#20904'
    const KEEL = 'steiner385/keel#65'
    const conflictRow = (id: string) => ({
      id, title: `title of ${id}`,
      actions: [{ kind: 'merge_conflict', label: 'Resolve conflicts', style: 'risky' }],
    })
    const job = (id: string, target: string, over: Record<string, unknown>) => ({
      job_id: id, target, kind: 'merge_conflict', status: 'running', phase: 'assessing',
      session_id: null, error_phase: null, message: '', level: 'ok', ...over,
    })
    const rowOf = (id: string) => screen.getByText(`title of ${id}`).closest('li') as HTMLElement
    const fire = (id: string) => {
      const button = within(rowOf(id)).getByRole('button', { name: /Resolve conflicts/ })
      fireEvent.click(button) // arm
      fireEvent.click(within(rowOf(id)).getByRole('button', { name: /Confirm Resolve conflicts/ }))
    }

    function deferred<T>() {
      let resolve!: (v: T) => void
      const promise = new Promise<T>((r) => { resolve = r })
      return { promise, resolve }
    }

    it('shows each job outcome on its own row and names the item in the toast', async () => {
      items = [conflictRow(KINDASH), conflictRow(KEEL)]
      mutateAsync.mockImplementation(async ({ item_id }: { item_id: string }) => ({
        status: 'queued', level: 'ok', message: 'Queued', phase: 'queued',
        job_id: item_id === KINDASH ? 'prd-kindash' : 'prd-keel',
      }))
      const kindashJob = deferred<unknown>()
      fetchPrDoctorJob.mockImplementation((id: string) => id === 'prd-kindash'
        ? kindashJob.promise
        : Promise.resolve(job('prd-keel', KEEL, {
          status: 'error', level: 'warn',
          message: "Not fixing: the PR's author is not a trusted author" })))
      render(<AutomationActions automationId="pr-doctor" enabled variant="full" />)

      fire(KINDASH)
      fire(KEEL)
      // keel#65's refusal settles first — on keel's row only, and its toast says so.
      expect(await within(rowOf(KEEL)).findByText(/not a trusted author/)).toBeInTheDocument()
      expect(within(rowOf(KINDASH)).queryByText(/not a trusted author/)).toBeNull()
      expect(toast.warning).toHaveBeenCalledWith(
        `${KEEL}: Not fixing: the PR's author is not a trusted author`)
      expect(within(rowOf(KINDASH)).getByRole('button', { name: /Queued…/ })).toBeDisabled()

      await act(async () => {
        kindashJob.resolve(job('prd-kindash', KINDASH, {
          status: 'ready', phase: 'ready', level: 'ok', message: 'Spawned fix session' }))
      })
      expect(within(rowOf(KINDASH)).getByText('Spawned fix session')).toBeInTheDocument()
      expect(within(rowOf(KEEL)).queryByText('Spawned fix session')).toBeNull()
      expect(toast.success).toHaveBeenCalledWith(`${KINDASH}: Spawned fix session`)
    })

    it('still reports a job whose button a refetch removed', async () => {
      items = [conflictRow(KINDASH)]
      mutateAsync.mockResolvedValue({
        status: 'queued', level: 'ok', message: 'Queued', phase: 'queued', job_id: 'prd-kindash' })
      const kindashJob = deferred<unknown>()
      fetchPrDoctorJob.mockReturnValue(kindashJob.promise)
      const { rerender } = render(<AutomationActions automationId="pr-doctor" enabled variant="full" />)
      fire(KINDASH)
      await waitFor(() => expect(fetchPrDoctorJob).toHaveBeenCalled())

      // The refetch after the click: the row is now owned by the new session.
      items = [{ id: KINDASH, title: `title of ${KINDASH}`, actions: [], status: 'Being worked by session b4a37f28' }]
      rerender(<AutomationActions automationId="pr-doctor" enabled variant="full" />)
      expect(screen.queryByRole('button')).toBeNull()

      await act(async () => {
        kindashJob.resolve(job('prd-kindash', KINDASH, {
          status: 'ready', phase: 'ready', level: 'ok', message: 'Spawned fix session' }))
      })
      expect(within(rowOf(KINDASH)).getByText('Spawned fix session')).toBeInTheDocument()
      expect(toast.success).toHaveBeenCalledWith(`${KINDASH}: Spawned fix session`)
    })

    it('never adopts an answer about another target', async () => {
      items = [conflictRow(KINDASH)]
      mutateAsync.mockResolvedValue({
        status: 'queued', level: 'ok', message: 'Queued', phase: 'queued', job_id: 'prd-kindash' })
      fetchPrDoctorJob.mockResolvedValue(job('prd-kindash', KEEL, {
        status: 'error', level: 'warn', message: "Not fixing: the PR's author is not a trusted author" }))
      render(<AutomationActions automationId="pr-doctor" enabled variant="full" />)
      fire(KINDASH)
      expect(await within(rowOf(KINDASH)).findByText(/Lost track of the job/)).toBeInTheDocument()
      expect(screen.queryByText(/not a trusted author/)).toBeNull()
    })

    it('an outcome expires off its row', async () => {
      vi.useFakeTimers()
      items = [{ id: 'fp1', title: 'a signature', actions: [{ kind: 'file_now', label: 'File now' }] }]
      mutateAsync.mockResolvedValue({ status: 'filed', level: 'error', message: 'Could not file' })
      render(<AutomationActions automationId="error-triage" enabled variant="full" />)
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'File now' })) })
      expect(screen.getByText('Could not file')).toBeInTheDocument()
      // Not a PR ref → the toast names the item by its title.
      expect(toast.error).toHaveBeenCalledWith('a signature: Could not file')
      await act(async () => { vi.advanceTimersByTime(OUTCOME_TTL_MS + 1) })
      expect(screen.queryByText('Could not file')).toBeNull()
    })
  })
})
