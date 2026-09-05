import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PageHeader } from './PageHeader'
import { PageContent } from './PageContent'
import { EmptyState } from './EmptyState'
import { Skeleton } from './Skeleton'
import { resetDocumentTitle } from '@/shell/documentTitle'
import { InteractionProvider } from '@/shell/InteractionContext'

describe('page primitives', () => {
  beforeEach(resetDocumentTitle)

  it('PageHeader renders the h1 and sets document.title', () => {
    render(<PageHeader title="Automations" actions={<button>New</button>} />)
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Automations')
    expect(document.title).toBe('Automations — ccswitch')
  })

  it('PageHeader can title the document differently from its h1', () => {
    // Settings: one page called "Settings", whose TAB says which section is open.
    render(<PageHeader title="Settings" documentTitle="Defaults · Settings" />)
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Settings')
    expect(document.title).toBe('Defaults · Settings — ccswitch')
  })

  it('PageHeader renders meta and actions', () => {
    render(<PageHeader title="Automations" meta="3 active" actions={<button>New</button>} />)
    expect(screen.getByText('3 active')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New' })).toBeInTheDocument()
  })

  it('PageHeader updates document.title when the title prop changes', () => {
    const { rerender } = render(<PageHeader title="Automations" />)
    expect(document.title).toBe('Automations — ccswitch')
    rerender(<PageHeader title="Sessions" />)
    expect(document.title).toBe('Sessions — ccswitch')
  })

  it("a stale unmount does not clobber a newer PageHeader's title (route-transition race)", () => {
    // This is the exact scenario pageHeaderSeq exists for: an outgoing page
    // (A) is still mounted — e.g. mid exit-animation — when the incoming
    // page (B) mounts and claims the title. A naive cleanup that
    // unconditionally resets document.title on unmount would have A's
    // *later* unmount stomp B's already-correct title back to a stale
    // value. The ordering below (mount A, mount B while A is still up,
    // THEN unmount A) is what makes this test meaningful — it fails against
    // an unconditional-reset cleanup and passes only because the ownership
    // counter lets A's cleanup recognize it's been superseded.
    const a = render(<PageHeader title="Old Page" />)
    expect(document.title).toBe('Old Page — ccswitch')
    render(<PageHeader title="New Page" />)
    expect(document.title).toBe('New Page — ccswitch')
    a.unmount()
    expect(document.title).toBe('New Page — ccswitch')
  })

  it('an inner header unmounting restores the still-mounted outer header\'s title', () => {
    // The reverse of the race above: B (newer) mounts on top of A (older,
    // still mounted and visible), then B unmounts first while A is still on
    // screen. The title should return to A's, not fall back to the bare app
    // name.
    render(<PageHeader title="Alpha" />)
    expect(document.title).toBe('Alpha — ccswitch')
    const b = render(<PageHeader title="Beta" />)
    expect(document.title).toBe('Beta — ccswitch')
    b.unmount()
    expect(document.title).toBe('Alpha — ccswitch')
  })

  it('PageContent exposes busy state and Skeleton is decorative', () => {
    const { rerender } = render(<PageContent busy><Skeleton lines={3} /></PageContent>)
    expect(screen.getByTestId('page-content')).toHaveAttribute('aria-busy', 'true')
    expect(document.querySelector('[aria-hidden="true"] .animate-pulse')).not.toBeNull()
    rerender(<PageContent><EmptyState title="No automations yet" /></PageContent>)
    expect(screen.getByRole('status')).toHaveTextContent('Loaded')
    expect(screen.getByText('No automations yet')).toBeInTheDocument()
  })

  it('PageContent supports the non-scrolling (workspace) variant', () => {
    render(<PageContent scroll={false}><div>panes scroll themselves</div></PageContent>)
    const el = screen.getByTestId('page-content')
    expect(el.className).toContain('overflow-hidden')
    expect(el.className).not.toContain('overflow-y-auto')
  })

  it('Skeleton renders the requested number of decorative rows', () => {
    const { container } = render(<Skeleton lines={5} />)
    expect(container.querySelectorAll('.animate-pulse')).toHaveLength(5)
  })

  it('EmptyState renders icon and description', () => {
    render(
      <EmptyState
        icon={<span data-testid="empty-icon" />}
        title="No automations yet"
        description="Create one to get started"
      />,
    )
    expect(screen.getByTestId('empty-icon')).toBeInTheDocument()
    expect(screen.getByText('Create one to get started')).toBeInTheDocument()
  })
})

/**
 * Task 19: `page.header` actions are the interactive half of the header, so
 * kiosk drops them. The title and `meta` stay — a wall display exists to show
 * exactly those.
 *
 * The `interactive={true}` case is asserted alongside the false one, and the
 * `beforeEach` above is inherited by neither describe, so `resetDocumentTitle`
 * runs here too via this block's own hook.
 */
describe('PageHeader — read-only (kiosk) actions', () => {
  beforeEach(resetDocumentTitle)

  it('renders actions under interactive={true}', () => {
    render(
      <InteractionProvider interactive={true}>
        <PageHeader title="Automations" meta="3 active" actions={<button>New</button>} />
      </InteractionProvider>,
    )
    expect(screen.getByRole('button', { name: 'New' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Automations')
    expect(screen.getByText('3 active')).toBeInTheDocument()
  })

  it('drops actions under interactive={false} but keeps the title and meta', () => {
    render(
      <InteractionProvider interactive={false}>
        <PageHeader title="Automations" meta="3 active" actions={<button>New</button>} />
      </InteractionProvider>,
    )
    expect(screen.queryByRole('button', { name: 'New' })).toBeNull()
    // The positive lower bound: the header itself is still on screen, so
    // "no New button" cannot be satisfied by a header that failed to render.
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Automations')
    expect(screen.getByText('3 active')).toBeInTheDocument()
    expect(document.title).toBe('Automations — ccswitch')
  })
})
