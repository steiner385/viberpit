import * as React from 'react'
import { cn } from '@/lib/utils'

export interface BannerStackItem { id: string; priority: number; node: React.ReactNode }

/**
 * The one place banners render (region app.banners). Highest priority first; a null `node`
 * is skipped. Renders null when nothing is live, so mount sites need no [&:empty]:hidden
 * wrapper of their own.
 *
 * This exists to close a structural gap: banners used to be mounted separately per shell
 * (App.tsx, the mobile Dashboard, DesktopShell), which let a banner ship on one shell and
 * silently never reach the other. Every shell renders one BannerStack; a new banner is added
 * to its items list once and both shells pick it up.
 */
export function BannerStack({ items, className }: { items: BannerStackItem[]; className?: string }) {
  const visible = items.filter((i) => i.node != null).sort((a, b) => b.priority - a.priority)
  if (visible.length === 0) return null
  return (
    <div data-region="app.banners" className={cn('flex flex-col gap-2', className)}>
      {visible.map((i) => <React.Fragment key={i.id}>{i.node}</React.Fragment>)}
    </div>
  )
}
