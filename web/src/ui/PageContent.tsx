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
      className={cn('flex-1 min-h-0 flex flex-col', scroll ? 'overflow-y-auto px-4 py-4 md:px-6' : 'overflow-hidden', className)}
    >
      {children}
      <span role="status" aria-live="polite" className="sr-only">
        {busy ? 'Loading' : 'Loaded'}
      </span>
    </div>
  )
}
