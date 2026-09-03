import * as React from 'react'
import { cn } from '@/lib/utils'

export interface BannerStackItem { id: string; priority: number; node: React.ReactNode }

/**
 * The one place banners render (region app.banners). Highest priority first; a null `node`
 * is skipped. When all items render null internally, the wrapper renders with zero children
 * and is hidden by [&:empty]:hidden — it does not return null in this case.
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
    <div data-region="app.banners" className={cn('flex flex-col gap-2 [&:empty]:hidden', className)}>
      {visible.map((i) => <React.Fragment key={i.id}>{i.node}</React.Fragment>)}
    </div>
  )
}
