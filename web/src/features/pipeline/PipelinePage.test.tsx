import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, within, cleanup } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import { ViewportProvider, type ViewportKiosk } from '@/shell/useViewport'
import { InteractionProvider } from '@/shell/InteractionContext'
import { LAYOUT_MODE_QUERIES, MEDIA, stubMatchMedia } from '@/test/matchMedia'
import type { DashboardState } from '@/pipeline/types'
import { PipelinePage } from './PipelinePage'

vi.mock('@/api/hooks', () => ({
  useFixPr: () => ({ mutate: vi.fn(), isPending: false }),
}))

// jsdom has no EventSource — the same minimal stub `useDashboard.test.tsx` uses,
// plus `emit` so a test can deliver a frame.
class FakeEventSource {
  static instances: FakeEventSource[] = []
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  url: string
  constructor(url: string) { this.url = url; FakeEventSource.instances.push(this) }
  addEventListener() {}
  close() {}
  emit(frame: DashboardState) {
    this.onopen?.(new Event('open'))
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent)
  }
}

const frame = (): DashboardState => ({
  generatedAt: '2026-09-11T10:00:00Z',
  staleSince: null,
  repos: [
    { repo: 'o/a', hasDeploy: false, prs: [], queue: null },
    { repo: 'o/b', hasDeploy: false, prs: [], queue: null },
  ],
} as unknown as DashboardState)

// `useFocusedPipeline` now writes `?pipeline=` through react-router's
// useSearchParams (#1555), not raw history.replaceState — a MemoryRouter
// never touches the real `window.location`, so a write is only observable
// through the router's own `useLocation()`. This probe surfaces it.
function LocationSearchProbe() {
  const { search } = useLocation()
  return <span data-testid="loc-search">{search}</span>
}

function mount({ kiosk = null, entry = '/pipeline', portrait = false }: { kiosk?: ViewportKiosk | null; entry?: string; portrait?: boolean } = {}) {
  const queries = LAYOUT_MODE_QUERIES[kiosk ? 'kiosk' : 'desktop']
  stubMatchMedia(portrait ? [...queries, MEDIA.portrait] : queries)
  const Wrap = ({ children }: { children: ReactNode }) => (
    <ViewportProvider kiosk={kiosk}>
      <InteractionProvider interactive={!kiosk}>
        <MemoryRouter initialEntries={[entry]}>
          {children}
          <LocationSearchProbe />
        </MemoryRouter>
      </InteractionProvider>
    </ViewportProvider>
  )
  return render(<PipelinePage />, { wrapper: Wrap })
}

const focusedPipelineParam = () => new URLSearchParams(screen.getByTestId('loc-search').textContent ?? '').get('pipeline')

const deliver = (f: DashboardState = frame()) => act(() => { FakeEventSource.instances.at(-1)!.emit(f) })

/** A minimal PR view at one stage — enough for `bucketPr` and the row. */
const prAt = (repo: string, number: number, stage: string, substate: string | null = null) => ({
  repo, number, title: `pr ${number}`, url: `https://x/${number}`,
  stage: { stage, substate, percent: null, etaSeconds: null, etaRangeSeconds: null, overdue: false },
  queueAheadCount: null, checks: [], groupChecks: null, mergeEtaSim: null,
})

/** `o/a` (focused first) has only a running PR; the one red PR lives in `o/b`. */
const redInSecondRepo = (): DashboardState => ({
  generatedAt: '2026-09-11T10:00:00Z',
  staleSince: null,
  repos: [
    { repo: 'o/a', hasDeploy: false, prs: [prAt('o/a', 1, 'ci')], queue: null },
    { repo: 'o/b', hasDeploy: false, prs: [prAt('o/b', 2, 'parked', 'ci-failed')], queue: null },
  ],
} as unknown as DashboardState)

describe('PipelinePage (Workspace variant)', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
    vi.stubGlobal('Notification', undefined)
    localStorage.clear()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('is on the page contract before the first frame: one h1, the title, the variant, a loading status', () => {
    mount()
    expect(document.querySelector('[data-page-variant="workspace"]')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'Pipeline' })).toBeInTheDocument()
    expect(document.title).toMatch(/^Pipeline/)
    // `PageContent` owns a second `role="status"` (its "Loaded" announcer), so
    // the loading announcement is found by text and checked for its live role.
    expect(screen.getByText('Loading pipeline…').closest('[role="status"]')).toBeInTheDocument()
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
  })

  it('a feed that only errors shows the danger banner after the stale window', () => {
    vi.useFakeTimers()
    mount()
    act(() => { FakeEventSource.instances.at(-1)!.onerror?.(new Event('error')) })
    expect(screen.queryByText(/Pipeline feed unavailable/)).not.toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(90_001) })
    expect(screen.getByText('Pipeline feed unavailable — reconnecting…')).toBeInTheDocument()
  })

  it('after a frame: the repo strip is a tablist, the first repo is focused, and a tab click rewrites ?pipeline=', () => {
    mount()
    deliver()
    const tablist = screen.getByRole('tablist', { name: 'Pipeline repository' })
    const tabs = within(tablist).getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual(['o/a', 'o/b'])
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(tabs[1]!)
    expect(tabs[1]).toHaveAttribute('aria-selected', 'true')
    expect(focusedPipelineParam()).toBe('o/b')
    // The board now leads with the focused repo.
    expect(screen.getAllByRole('heading', { level: 3 })[0]).toHaveTextContent('o/b')
    // The health band sits in the header, not the board.
    expect(screen.getByRole('group', { name: 'Overall CI health' })).toBeInTheDocument()
  })

  // 2026-09-30 Pipeline critique, P1: the header's lanes are cross-repo, the
  // strip and rows are the focused repo's. A jump used to filter the focused
  // repo down to "0 FAILED" while the chip said "1 red" and the red row sat in
  // another repo.
  describe('a lane jump lands on the repo that owns the attention', () => {
    it('moves the focus to the repo with the red row when the focused repo has none', () => {
      mount()
      deliver(redInSecondRepo())
      expect(focusedPipelineParam()).toBeNull() // first repo, implicit
      fireEvent.click(screen.getByTestId('health-lane-pr-ci'))
      expect(focusedPipelineParam()).toBe('o/b')
      expect(screen.getAllByRole('heading', { level: 3 })[0]).toHaveTextContent('o/b')
      // …and the Failed tile is the active filter, counting that repo's red row.
      const failed = screen.getByRole('button', { name: /Failed/ })
      expect(failed).toHaveAttribute('aria-pressed', 'true')
      expect(failed).toHaveTextContent('1')
      expect(screen.getByText('pr 2')).toBeInTheDocument()
    })

    it('follows the RED row past a focused repo whose Failed tile counts only stuck PRs', () => {
      const f = redInSecondRepo()
      f.repos[0]!.prs.push(prAt('o/a', 3, 'ready', 'stuck-enqueue') as never) // Failed bucket, not red
      mount()
      deliver(f)
      fireEvent.click(screen.getByTestId('health-lane-pr-ci'))
      expect(focusedPipelineParam()).toBe('o/b')
    })

    it('stays on the focused repo when it already owns a row in that bucket', () => {
      mount({ entry: '/pipeline?pipeline=o%2Fb' })
      deliver(redInSecondRepo())
      fireEvent.click(screen.getByTestId('health-lane-pr-ci'))
      expect(focusedPipelineParam()).toBe('o/b')
    })

    it('the rollup jumps the same way', () => {
      mount()
      deliver(redInSecondRepo())
      fireEvent.click(screen.getByTestId('health-rollup'))
      expect(focusedPipelineParam()).toBe('o/b')
    })
  })

  it('the bell is a header action that asks for permission when first enabled', () => {
    const requestPermission = vi.fn().mockResolvedValue('granted')
    vi.stubGlobal('Notification', { permission: 'default', requestPermission })
    mount()
    const bell = screen.getByRole('button', { name: 'Browser notifications (this tab)' })
    expect(bell).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(bell)
    expect(requestPermission).toHaveBeenCalled()
  })

  it('the legend is a header action that opens the legend dialog', () => {
    mount()
    fireEvent.click(screen.getByRole('button', { name: 'Legend' }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  // 2026-09-30 Pipeline critique (heuristic 1 / kiosk P2): every number on the
  // board is only as fresh as the frame, and nothing said when that was.
  it('the header says when the frame was computed, in local time, on every layout', () => {
    const expected = `as of ${new Date('2026-09-11T10:00:00Z').toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false })}`
    mount()
    expect(screen.queryByTestId('pipeline-as-of')).toBeNull() // no frame yet, nothing to date
    deliver()
    expect(screen.getByTestId('pipeline-as-of')).toHaveTextContent(expected)
    expect(screen.getByTestId('pipeline-as-of')).toHaveAttribute('dateTime', '2026-09-11T10:00:00Z')
    cleanup()
    mount({ kiosk: { enabled: true, cycleSeconds: 10, pages: ['pipeline'] } })
    deliver()
    expect(screen.getByTestId('pipeline-as-of')).toHaveTextContent(expected)
  })

  it('kiosk: no repo strip, no header actions, and the focused repo cycles', () => {
    vi.useFakeTimers()
    mount({ kiosk: { enabled: true, cycleSeconds: 10, pages: ['pipeline'] } })
    deliver()
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Legend' })).not.toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 3 })[0]).toHaveTextContent('o/a')
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(screen.getAllByRole('heading', { level: 3 })[0]).toHaveTextContent('o/b')
    // and the URL was never written: the kiosk URL is its configuration
    expect(focusedPipelineParam()).toBeNull()
  })

  it('kiosk: a ?pipeline= deep link still seeds the starting repo (#1555)', () => {
    mount({ kiosk: { enabled: true, cycleSeconds: 10, pages: ['pipeline'] }, entry: '/pipeline?pipeline=o%2Fb' })
    deliver()
    expect(screen.getAllByRole('heading', { level: 3 })[0]).toHaveTextContent('o/b')
  })

  // #1407 (G09, FB-2): PipelinePage threads useViewport().portrait — the one
  // orientation signal — into the board so a portrait kiosk wall display
  // doesn't depend on pointer-only horizontal scroll (queue-train wraps under
  // .pipeline-portrait; see plotroom.test.ts for the CSS side).
  it('a portrait kiosk viewport marks the board .pipeline-portrait; landscape does not', () => {
    mount({ kiosk: { enabled: true, cycleSeconds: 10, pages: ['pipeline'] }, portrait: true })
    deliver()
    expect(document.querySelector('.pipeline-portrait')).toBeInTheDocument()
  })

  it('landscape kiosk leaves the board in its default (no .pipeline-portrait) composition', () => {
    mount({ kiosk: { enabled: true, cycleSeconds: 10, pages: ['pipeline'] } })
    deliver()
    expect(document.querySelector('.pipeline-portrait')).not.toBeInTheDocument()
  })

  it('a stalled feed shows a warning banner as the first thing in the content region', () => {
    vi.useFakeTimers()
    mount()
    deliver()
    expect(screen.queryByText(/feed stalled/)).not.toBeInTheDocument()
    act(() => { vi.advanceTimersByTime(90_000) })
    const banner = screen.getByText(/Pipeline feed stalled/).closest('[role="status"]')
    expect(banner).toBeInTheDocument()
    // First thing in the content region: the banner precedes the board.
    const content = banner!.parentElement!
    expect(content.firstElementChild).toBe(banner)
  })
})
