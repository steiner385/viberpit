import { HelpCircle, Sparkles } from 'lucide-react'
import { BrandMark, Wordmark } from '@/shell/BrandMark'
import { useAssistantNotices } from '@/api/assistant'
import { useOpenDrawer } from '@/shell/drawerParam'
import { NavItem } from './NavItem'

/**
 * `app.rail` on phone, top half: brand on the left, Assistant and Help on the
 * right. Pairs with `NavBar` (see its note on why `app.rail` appears twice on
 * phone and what `data-rail` is for).
 *
 * It deliberately renders NO `h1`. The page title is the page's own `h1`, owned
 * by `PageHeader`; a second one here would give every phone screen two level-1
 * headings and break the page contract. The brand is the `Wordmark` beside the
 * mark — `BrandMark` is `aria-hidden` and `Wordmark` carries the full name as
 * one visually-hidden node, so the word "ViberPit" is what a screen reader
 * announces, exactly as in the header this replaces.
 */
export function TopBar() {
  const openDrawer = useOpenDrawer()
  const unreadNotices = useAssistantNotices().data?.unread ?? 0

  return (
    <header
      data-region="app.rail"
      data-rail="top"
      // `short:hidden` (Shed-Not-Shrink): with the keyboard up or the phone on
      // its side, 61px of brand plus two buttons the More sheet also carries is
      // the chrome to give back — at 390×560 the Sessions list was 207px (one
      // card) and at 640×400 (a desktop at 200% zoom) 99px with a 29px
      // scroller under it (2026-10-01 shell critique, P2). Nothing is lost:
      // Assistant and Help are in `NavBar`'s More sheet, and the brand is not
      // navigation.
      className="z-nav flex shrink-0 items-center gap-2 border-b border-edge bg-surface px-3 pb-2 short:hidden"
      // Clears the iOS status bar; env() is 0 elsewhere. Inline style to match
      // the pre-shell mobile chrome.
      style={{ paddingTop: 'calc(env(safe-area-inset-top) + 0.5rem)' }}
    >
      <BrandMark size={22} />
      <Wordmark className="text-base font-bold tracking-tight" />
      <div className="flex-1" />
      <NavItem
        onClick={() => openDrawer('assistant')}
        icon={Sparkles}
        label="Assistant"
        count={unreadNotices}
        attention={unreadNotices > 0}
      />
      <NavItem onClick={() => openDrawer('help')} icon={HelpCircle} label="Help" />
    </header>
  )
}
