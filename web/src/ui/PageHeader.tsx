import * as React from 'react'
import { cn } from '@/lib/utils'
import { setBaseTitle, releaseBaseTitle } from '@/shell/documentTitle'
import { useInteractive } from '@/shell/InteractionContext'

export interface PageHeaderProps {
  title: string
  meta?: React.ReactNode
  actions?: React.ReactNode
  className?: string
}

/** Zone 1 of every page: the only h1 on the page, synced to document.title (WCAG 2.4.2). */
export function PageHeader({ title, meta, actions, className }: PageHeaderProps) {
  // A stable per-instance identity for `shell/documentTitle.ts`'s base-title
  // stack. useState's initializer (not a ref) because this codebase's
  // react-hooks/refs rule rejects reading a ref during render.
  const [token] = React.useState(() => Symbol('page-header'))

  // `actions` is the page's action cluster (New, Refresh, a menu). Kiosk is
  // read-only, so it is not rendered at all rather than rendered disabled: a row
  // of greyed-out buttons on a wall display is chrome that costs header width
  // and invites a passer-by to poke at it. The title and `meta` stay — they are
  // what the display is FOR.
  const interactive = useInteractive()

  // Claim/update this instance's slot on every title change...
  React.useEffect(() => {
    setBaseTitle(token, title)
  }, [token, title])

  // ...and release it exactly once, on unmount. Split from the effect above
  // so a title change (which re-runs the effect) doesn't release-then-reclaim
  // this instance's slot — that would pop it off the stack and push it back
  // on top, reordering it ahead of a PageHeader mounted after it while both
  // are still visible during a route transition.
  React.useEffect(() => {
    return () => releaseBaseTitle(token)
  }, [token])

  return (
    <header className={cn('shrink-0 flex items-center gap-3 px-4 py-3 md:px-6 md:py-4 border-b border-edge', className)}>
      <div className="min-w-0 flex-1">
        <h1 className="text-lg font-semibold text-ink truncate">{title}</h1>
        {meta && <div className="text-sm text-ink-muted">{meta}</div>}
      </div>
      {interactive && actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </header>
  )
}
