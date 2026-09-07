import * as React from 'react'
import { cn } from '@/lib/utils'
import { setBaseTitle, releaseBaseTitle } from '@/shell/documentTitle'
import { useInteractive } from '@/shell/InteractionContext'
import { useToastAnchor } from '@/shell/toastInsets'

export interface PageHeaderProps {
  title: string
  /**
   * The base for `document.title`, when it must differ from the visible `h1`.
   *
   * Settings is the case this exists for: the page is one page called
   * "Settings", so its `h1` must not change under the user as they move between
   * sections — but a browser tab (or a window switcher) showing three tabs all
   * reading "Settings — ViberPit" is useless, so the TITLE carries the section
   * ("Defaults · Settings — ViberPit"). WCAG 2.4.2 asks the title to identify
   * the page; it does not ask it to be byte-identical to the heading.
   *
   * Defaults to `title`, so every other page is unaffected.
   */
  documentTitle?: string
  meta?: React.ReactNode
  actions?: React.ReactNode
  className?: string
}

/** Zone 1 of every page: the only h1 on the page, synced to document.title (WCAG 2.4.2). */
export function PageHeader({ title, documentTitle, meta, actions, className }: PageHeaderProps) {
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

  // A toast must never cover the title or the action cluster: the header
  // publishes its bottom edge and the toaster starts below it (toastInsets).
  const clearRef = useToastAnchor('clear')

  // Claim/update this instance's slot on every title change...
  const base = documentTitle ?? title
  React.useEffect(() => {
    setBaseTitle(token, base)
  }, [token, base])

  // ...and release it exactly once, on unmount. Split from the effect above
  // so a title change (which re-runs the effect) doesn't release-then-reclaim
  // this instance's slot — that would pop it off the stack and push it back
  // on top, reordering it ahead of a PageHeader mounted after it while both
  // are still visible during a route transition.
  React.useEffect(() => {
    return () => releaseBaseTitle(token)
  }, [token])

  return (
    <header ref={clearRef} className={cn('shrink-0 flex items-center gap-3 px-4 py-3 md:px-6 md:py-4 border-b border-edge', className)}>
      <div className="min-w-0 flex-1">
        <h1 className="text-lg font-semibold text-ink truncate">{title}</h1>
        {meta && <div className="text-sm text-ink-muted">{meta}</div>}
      </div>
      {interactive && actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </header>
  )
}
