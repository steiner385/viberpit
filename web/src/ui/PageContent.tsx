import * as React from 'react'
import { cn } from '@/lib/utils'

export interface PageContentProps {
  busy?: boolean
  /** Default true. Set false for workspace-style pages whose inner panes scroll themselves. */
  scroll?: boolean
  className?: string
  children: React.ReactNode
}

/** Zone 3 of every page: owns scrolling for Standard/Split/Reader layouts, or
 *  clips (scroll=false) for a Workspace layout whose inner panes scroll
 *  themselves. Carries aria-busy while loading and a role="status" live
 *  region that announces when content arrives, for screen-reader users who
 *  can't see the Skeleton (which is aria-hidden). */
export function PageContent({ busy = false, scroll = true, className, children }: PageContentProps) {
  return (
    <div
      data-testid="page-content"
      aria-busy={busy || undefined}
      // A virtualized list inside a page finds its scroll container by this
      // attribute (LeafList); a Workspace page (scroll=false) has none — its
      // panes scroll themselves.
      {...(scroll ? { 'data-page-scroll': '' } : {})}
      // A scroll container with no focusable child (About, long read-only
      // copy) cannot be scrolled from the keyboard outside Chromium, which
      // alone adds a scroller to the tab order natively. An explicit tab stop
      // gives Tab/PageDown/Space the same reach everywhere (WCAG 2.1.1, axe
      // `scrollable-region-focusable`).
      {...(scroll ? { tabIndex: 0 } : {})}
      className={cn(
        'flex-1 min-h-0 flex flex-col',
        scroll ? 'overflow-y-auto px-4 py-4 md:px-6 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent' : 'overflow-hidden',
        className,
      )}
    >
      {children}
      <span role="status" aria-live="polite" className="sr-only">
        {busy ? 'Loading' : 'Loaded'}
      </span>
    </div>
  )
}
