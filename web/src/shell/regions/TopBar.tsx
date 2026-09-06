import { HelpCircle, Sparkles } from 'lucide-react'
import { BrandMark } from '@/components/BrandMark'
import { BRAND_NAME } from '@/brand'
import { useOpenDrawer } from '@/shell/drawerParam'
import { NavItem } from './NavItem'

/**
 * `app.rail` on phone, top half: brand on the left, Assistant and Help on the
 * right. Pairs with `NavBar` (see its note on why `app.rail` appears twice on
 * phone and what `data-rail` is for).
 *
 * It deliberately renders NO `h1`. The page title is the page's own `h1`, owned
 * by `PageHeader`; a second one here would give every phone screen two level-1
 * headings and break the page contract. The brand is plain text beside the mark
 * — `BrandMark` is `aria-hidden`, so the word "ccswitch" is what a screen reader
 * announces, exactly as in the header this replaces.
 */
export function TopBar() {
  const openDrawer = useOpenDrawer()

  return (
    <header
      data-region="app.rail"
      data-rail="top"
      className="z-nav flex shrink-0 items-center gap-2 border-b border-edge bg-surface px-3 pb-2"
      // Clears the iOS status bar; env() is 0 elsewhere. Inline style to match
      // the existing mobile chrome (App.tsx, SessionView, ReviewView).
      style={{ paddingTop: 'calc(env(safe-area-inset-top) + 0.5rem)' }}
    >
      <BrandMark size={22} />
      <span className="text-base font-semibold tracking-tight">{BRAND_NAME}</span>
      <div className="flex-1" />
      <NavItem onClick={() => openDrawer('assistant')} icon={Sparkles} label="Assistant" />
      <NavItem onClick={() => openDrawer('help')} icon={HelpCircle} label="Help" />
    </header>
  )
}
