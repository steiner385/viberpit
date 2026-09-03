import * as React from 'react'
import { Link } from 'react-router-dom'
import type { LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Badge } from '@/ui/Badge'
import { badgedName } from '@/ui/badgedName'
import { useDrawerPreservingTo } from '@/shell/drawerParam'

/**
 * One nav control, rendered the same way everywhere in the shell.
 *
 * `NavRail` renders it, and `NavBar`/`TopBar` render it too — a second item
 * renderer would be the classic two-copies-of-one-concept bug (the badge
 * convention and the touch target would drift the first time either changed).
 *
 * Two shapes, one look:
 *  - `to`      → a react-router `<Link>`. Every route target uses this, never a
 *                `<button>` + `navigate()`, so a nav target stays middle-clickable
 *                and copyable.
 *  - `onClick` → a `<button>`, for controls that are not a destination (the
 *                drawer openers, the theme menu trigger).
 *
 * Accessible name: real text in the DOM, never an `aria-label` — that is the M6
 * lesson from PR 2, where an `aria-label` sitting beside a visible label was free
 * to drift from it. One visually-hidden span carries the whole `badgedName()`,
 * and the visible label and `Badge`s are `aria-hidden`; both are rendered from
 * the same props in this one component, so they cannot disagree.
 *
 * Badges are drawn in BOTH forms, and that is deliberate: `showLabel` decides
 * where the badge goes, never whether one exists. With a label it sits inline at
 * the end of the row; without one — the phone bar, the collapsed rail — it
 * overlays the icon's top-right corner, which is the only pixel a sighted user
 * has left. Gating it on `showLabel` silently deleted the count from every phone
 * screen, since `ui/Tabs.tsx` (the strip this replaces) always drew it.
 *
 * The name is one node rather than a visible label plus a hidden " (3)" because
 * the accname algorithm trims each element's contribution: sibling spans are
 * announced "Sessions(3)", which is not what the tab strip says today. Verified
 * against `dom-accessibility-api`, the same implementation Testing Library uses.
 */
interface NavItemBase {
  icon: LucideIcon
  label: string
  /** Count badge. Falsy (including 0) renders nothing — `useNavBadges` maps 0 → undefined. */
  count?: number
  /** Amber dot + "needs attention" in the accessible name. */
  attention?: boolean
  /** Render the label as visible text. It stays in the accessible name either way. */
  showLabel?: boolean
  /** Sets `aria-current="page"`. Exactly one item in a region may be active. */
  active?: boolean
  className?: string
}

export type NavItemProps =
  | (NavItemBase & { to: string; onClick?: never; buttonRef?: never })
  | (NavItemBase & {
      onClick: () => void
      to?: never
      /** Ref onto the rendered `<button>` — used to anchor a `Menu`. */
      buttonRef?: React.Ref<HTMLButtonElement>
    })

const ITEM_CLASS =
  'relative flex items-center gap-2 rounded-md px-2 min-h-target min-w-target text-sm font-medium transition-colors ' +
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent'

export function NavItem(props: NavItemProps) {
  const { icon: Icon, label, count, attention, showLabel = false, active = false, className } = props
  const name = badgedName({ label, count, attention })
  // An open drawer survives a section change (sub-spec B §4.2). Applied here
  // because this is where a nav target becomes an href — see `useDrawerPreservingTo`.
  const withOpenDrawer = useDrawerPreservingTo()

  const body = (
    <>
      <Icon size={20} aria-hidden="true" className="shrink-0" />
      {showLabel && <span aria-hidden="true" className="truncate">{label}</span>}
      <span className="sr-only">{name}</span>
      {showLabel ? (
        <>
          {attention && <Badge tone="warning" dot />}
          {count ? <Badge tone="neutral" count={count} aria-hidden="true" className="ml-auto" /> : null}
        </>
      ) : count ? (
        // Label-less: the badge overlays the icon's top-right corner, and when
        // the item also needs attention it carries the attention tone rather
        // than sharing that corner with a dot.
        <Badge
          tone={attention ? 'warning' : 'neutral'}
          count={count}
          aria-hidden="true"
          className="absolute top-0.5 right-0.5 px-1 text-2xs"
        />
      ) : attention ? (
        <Badge tone="warning" dot className="absolute top-1 right-1" />
      ) : null}
    </>
  )

  const classes = cn(
    ITEM_CLASS,
    active ? 'bg-accent-wash text-accent' : 'text-ink-muted hover:bg-surface-raised hover:text-ink',
    showLabel ? 'justify-start' : 'justify-center',
    className,
  )

  if (props.to !== undefined) {
    return (
      <Link to={withOpenDrawer(props.to)} aria-current={active ? 'page' : undefined} className={classes}>
        {body}
      </Link>
    )
  }
  const { onClick, buttonRef } = props
  return (
    <button ref={buttonRef} type="button" onClick={onClick} className={classes}>
      {body}
    </button>
  )
}
