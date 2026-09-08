import * as React from 'react'
import { cn } from '@/lib/utils'
import type { LayoutMode } from '@/shell/useViewport'

export interface BannerStackItem {
  id: string
  priority: number
  node: React.ReactNode
  /** Used by the shell to filter the stack (kiosk shows `danger` only).
   *  Optional so existing call sites keep compiling; treated as 'info'. */
  tone?: 'info' | 'warning' | 'danger'
  /**
   * Layout modes this banner is too costly to show in — applied by
   * `bannersForMode`. A phone's banner strip competes with the content it sits
   * above, so a tall banner that has a home elsewhere in the app (deploy →
   * Settings › Deploy) names `'phone'` here.
   *
   * Declared on the ITEM rather than inside the banner component on purpose:
   * `useAppBanners` is the one place the stack is described, so "where does this
   * banner appear" stays answerable by reading that list. A component that hid
   * itself with `hidden md:block` would be invisible to every rule and test that
   * reasons about the stack — and would still occupy the stack's `gap`.
   */
  hideOn?: readonly LayoutMode[]
}

/**
 * The one *component* banners render through (region app.banners) — not one instance: App
 * mounts one, and so does whichever of Dashboard (mobile) or DesktopShell is active, so up
 * to three can be live at once. Highest priority first; a null `node` is skipped. When all
 * items render null internally, the wrapper renders with zero children and is hidden by
 * [&:empty]:hidden — it does not return null in this case.
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
    // [&:empty]:hidden is load-bearing, not cosmetic: an item whose component returns null
    // still counts as visible here, so the wrapper renders and its gap/padding would leave an
    // 8px strip on every screen (ruling R26). Banner.test.tsx pins the class by name.
    <div data-region="app.banners" className={cn('flex flex-col gap-2 [&:empty]:hidden', className)}>
      {visible.map((i) => <React.Fragment key={i.id}>{i.node}</React.Fragment>)}
    </div>
  )
}
