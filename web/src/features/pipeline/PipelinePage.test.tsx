import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'
import { ViewportProvider, type ViewportKiosk } from '@/shell/useViewport'
import { InteractionProvider } from '@/shell/InteractionContext'
import { LAYOUT_MODE_QUERIES, stubMatchMedia } from '@/test/matchMedia'
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

function mount({ kiosk = null, entry = '/pipeline' }: { kiosk?: ViewportKiosk | null; entry?: string } = {}) {
  stubMatchMedia(LAYOUT_MODE_QUERIES[kiosk ? 'kiosk' : 'desktop'])
  const Wrap = ({ children }: { children: ReactNode }) => (
    <ViewportProvider kiosk={kiosk}>
      <InteractionProvider interactive={!kiosk}>
        <MemoryRouter initialEntries={[entry]}>{children}</MemoryRouter>
      </InteractionProvider>
    </ViewportProvider>
  )
  return render(<PipelinePage />, { wrapper: Wrap })
}

const deliver = () => act(() => { FakeEventSource.instances.at(-1)!.emit(frame()) })

describe('PipelinePage (Workspace variant)', () => {
  beforeEach(() => {
    FakeEventSource.instances = []
    vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource)
    vi.stubGlobal('Notification', undefined)
    localStorage.clear()
    history.replaceState(null, '', '/pipeline')
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

  it('after a frame: the repo strip is a tablist, the first repo is focused, and a tab click rewrites ?pipeline=', () => {
    mount()
    deliver()
    const tablist = screen.getByRole('tablist', { name: 'Pipeline repository' })
    const tabs = within(tablist).getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual(['o/a', 'o/b'])
    expect(tabs[0]).toHaveAttribute('aria-selected', 'true')
    fireEvent.click(tabs[1]!)
    expect(tabs[1]).toHaveAttribute('aria-selected', 'true')
    expect(new URLSearchParams(location.search).get('pipeline')).toBe('o/b')
    // The board now leads with the focused repo.
    expect(screen.getAllByRole('heading', { level: 3 })[0]).toHaveTextContent('o/b')
    // The health band sits in the header, not the board.
    expect(screen.getByRole('group', { name: 'Overall CI health' })).toBeInTheDocument()
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
    expect(new URLSearchParams(location.search).get('pipeline')).toBeNull()
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
