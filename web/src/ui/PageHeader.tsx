import * as React from 'react'
import { cn } from '@/lib/utils'

export interface PageHeaderProps {
  title: string
  meta?: React.ReactNode
  actions?: React.ReactNode
  className?: string
}

const BASE_TITLE = 'ccswitch'

// Module-level sequence so the most-recently-mounted PageHeader always owns
// document.title. Two PageHeaders can briefly coexist during a route
// transition (an outgoing page mid-exit-animation while the incoming page
// has already mounted); without this, whichever one's *unmount* cleanup
// fires last would stomp the other's title. Each mount claims the next id;
// a cleanup only resets the title if it still owns the latest id — a stale
// unmount from an already-superseded PageHeader is a no-op instead of a
// clobber.
let pageHeaderSeq = 0

/** Zone 1 of every page: the only h1 on the page, synced to document.title (WCAG 2.4.2). */
export function PageHeader({ title, meta, actions, className }: PageHeaderProps) {
  React.useEffect(() => {
    // PageHeader takes ownership of document.title here (WCAG 2.4.2). But
    // web/src/hooks/useNeedsYouTitle.ts (consumed by Dashboard.tsx) ALSO
    // writes document.title today — unconditionally, on its own effect, and
    // its cleanup resets to the bare base title, which will clobber
    // whatever PageHeader just set. The two are not coordinated. This is
    // harmless right now only because PageHeader has no consumers yet.
    // Whoever first renders PageHeader on a page that also uses
    // useNeedsYouTitle MUST reconcile ownership — decide whether the
    // "needs you" prefix composes into the page title and which one wins —
    // before shipping that page.
    const id = ++pageHeaderSeq
    document.title = `${title} — ${BASE_TITLE}`
    return () => {
      if (pageHeaderSeq === id) document.title = BASE_TITLE
    }
  }, [title])

  return (
    <header className={cn('shrink-0 flex items-center gap-3 px-4 py-3 md:px-6 md:py-4 border-b border-edge', className)}>
      <div className="min-w-0 flex-1">
        <h1 className="text-lg font-semibold text-ink truncate">{title}</h1>
        {meta && <div className="text-sm text-ink-muted">{meta}</div>}
      </div>
      {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </header>
  )
}
