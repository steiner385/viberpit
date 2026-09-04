import * as React from 'react'
import { useLocation } from 'react-router-dom'
import { MoreHorizontal } from 'lucide-react'
import { Sheet } from '@/ui/Sheet'
import { NAV_BY_ID, NAV_DEFS, navDefForPath } from '@/shell/navDefs'
import { navBadgeProps, useNavBadges } from '@/shell/useNavBadges'
import { GlobalActions } from './GlobalActions'
import { NavItem } from './NavItem'

/**
 * `app.rail` on phone, bottom half: the primary sections plus More.
 *
 * Renders alongside `TopBar` and never alongside `NavRail`, so `app.rail`
 * legitimately appears twice on phone — `data-rail` ("bottom" here, "top" on
 * `TopBar`, "left" on `NavRail`) is what tells the two apart, and the region
 * singleton check is "at most one rail component per mode", not "at most one
 * `data-region="app.rail"` node".
 *
 * Controls are icon-only: the accessible name still carries the label and the
 * badge (`NavItem` → `badgedName`), the same as the collapsed rail. Five
 * horizontal icon+label controls do not fit a phone bar.
 *
 * The sections it cannot show live behind More, so More carries their badges
 * folded together — otherwise a count on a hidden section would be announced
 * nowhere at all.
 */

/** How many sections fit on the bar before More. The rest go in the sheet. */
const PRIMARY_COUNT = 4

export function NavBar() {
  const { pathname } = useLocation()
  const badges = useNavBadges()
  const [moreOpen, setMoreOpen] = React.useState(false)
  const moreRef = React.useRef<HTMLButtonElement>(null)
  const closeMore = React.useCallback(() => setMoreOpen(false), [])

  const activeId = navDefForPath(pathname)?.id
  const settings = NAV_BY_ID.settings

  const primary = NAV_DEFS.slice(0, PRIMARY_COUNT)
  const overflow = NAV_DEFS.slice(PRIMARY_COUNT)
  // Settings renders once, in the sheet's bottom cluster beside the global
  // actions — two links to one route would be two `aria-current` candidates.
  const overflowSections = overflow.filter((d) => d.id !== 'settings')

  // Every badge More hides, summed through the one badge convention.
  const hidden = overflow.reduce(
    (acc, def) => {
      const p = navBadgeProps(def, badges)
      return { count: acc.count + (p.count ?? 0), attention: acc.attention || p.attention === true }
    },
    { count: 0, attention: false },
  )

  return (
    <nav
      data-region="app.rail"
      data-rail="bottom"
      role="navigation"
      aria-label="Sections"
      className="z-nav flex shrink-0 items-stretch justify-around gap-1 border-t border-edge bg-surface px-2 pt-1"
      // env() is 0 on desktop; on a phone it clears the home/gesture bar. An
      // inline style, not a class: this is the mechanism the rest of the mobile
      // chrome already uses (App.tsx, Composer, DispatchBar), and an arbitrary
      // value class is banned in shell/** by ccswitch/no-raw-palette anyway.
      style={{ paddingBottom: 'calc(env(safe-area-inset-bottom) + 0.25rem)' }}
    >
      {primary.map((def) => (
        <NavItem
          key={def.id}
          to={def.path}
          icon={def.icon}
          label={def.label}
          active={activeId === def.id}
          {...navBadgeProps(def, badges)}
        />
      ))}

      <NavItem
        onClick={() => setMoreOpen(true)}
        buttonRef={moreRef}
        icon={MoreHorizontal}
        label="More"
        count={hidden.count || undefined}
        attention={hidden.attention}
      />

      <Sheet open={moreOpen} onClose={closeMore} title="More" side="bottom" returnFocusRef={moreRef}>
        <div className="flex flex-col gap-1">
          {overflowSections.map((def) => (
            <NavItem
              key={def.id}
              to={def.path}
              icon={def.icon}
              label={def.label}
              showLabel
              active={activeId === def.id}
              {...navBadgeProps(def, badges)}
            />
          ))}
          <GlobalActions showLabel />
          <NavItem
            to={settings.path}
            icon={settings.icon}
            label={settings.label}
            showLabel
            active={activeId === 'settings'}
            {...navBadgeProps(settings, badges)}
          />
        </div>
      </Sheet>
    </nav>
  )
}
