import { useLocation } from 'react-router-dom'
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import { cn } from '@/lib/utils'
import { IconButton } from '@/ui/IconButton'
import { NAV_BY_ID, NAV_DEFS, navDefForPath } from '@/shell/navDefs'
import { navBadgeProps, useNavBadges } from '@/shell/useNavBadges'
import { useViewport } from '@/shell/useViewport'
import { useRailExpanded } from '@/shell/railExpanded'
import { GlobalActions } from './GlobalActions'
import { NavItem } from './NavItem'

/**
 * `app.rail` — the left navigation for tablet and desktop.
 *
 * Sections come from `NAV_DEFS` and badges from `useNavBadges()`; nothing about
 * the app map is restated here. Settings is deliberately excluded from the main
 * loop and rendered once in the bottom cluster: the sub-spec lists it in both
 * places, and two links to one route means two `aria-current` candidates, which
 * is an accessibility bug, not a convenience.
 *
 * Active detection goes through `navDefForPath(pathname)` rather than an
 * equality test, so `/sessions/abc` keeps Sessions lit.
 *
 * The bottom cluster's app-level controls live in `GlobalActions`, shared with
 * the phone `NavBar`'s More sheet.
 */

export function NavRail() {
  const { pathname } = useLocation()
  const badges = useNavBadges()
  const { width } = useViewport()

  const [expandedPref, setExpandedPref] = useRailExpanded()
  // Expansion is offered only at the wide tier; the stored preference survives a
  // trip through a narrower window instead of being cleared by it.
  const canExpand = width === 'wide'
  const expanded = canExpand && expandedPref
  const toggleExpanded = () => {
    setExpandedPref(!expandedPref)
  }

  const activeId = navDefForPath(pathname)?.id
  const settings = NAV_BY_ID.settings

  return (
    <nav
      data-region="app.rail"
      data-rail="left"
      role="navigation"
      aria-label="Sections"
      className={cn(
        'z-nav flex h-full shrink-0 flex-col gap-1 border-r border-edge bg-surface p-2',
        expanded ? 'w-56' : 'w-16',
      )}
    >
      {NAV_DEFS.filter((d) => d.id !== 'settings').map((def) => (
        <NavItem
          key={def.id}
          to={def.path}
          icon={def.icon}
          label={def.label}
          showLabel={expanded}
          active={activeId === def.id}
          {...navBadgeProps(def, badges)}
        />
      ))}

      <div className="flex-1" />

      <GlobalActions showLabel={expanded} />
      <NavItem
        to={settings.path}
        icon={settings.icon}
        label={settings.label}
        showLabel={expanded}
        active={activeId === 'settings'}
        {...navBadgeProps(settings, badges)}
      />

      {canExpand && (
        <IconButton
          variant="ghost"
          aria-label={expanded ? 'Collapse navigation' : 'Expand navigation'}
          aria-expanded={expanded}
          onClick={toggleExpanded}
          className={expanded ? 'self-end' : 'self-center'}
        >
          {expanded ? <PanelLeftClose size={20} aria-hidden="true" /> : <PanelLeftOpen size={20} aria-hidden="true" />}
        </IconButton>
      )}
    </nav>
  )
}
