import * as React from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import { Monitor, Moon, Plus, Sparkles, Sun, HelpCircle, PanelLeftClose, PanelLeftOpen, type LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'
import { IconButton } from '@/ui/IconButton'
import { Menu } from '@/ui/Menu'
import { NAV_BY_ID, NAV_DEFS, navDefForPath, type NavDef } from '@/shell/navDefs'
import { useNavBadges } from '@/shell/useNavBadges'
import { useViewport } from '@/shell/useViewport'
import { useTheme, type ThemeChoice } from '@/shell/theme'
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
 */

const RAIL_EXPANDED_KEY = 'ccswitch.railExpanded'

// `web/src/lib/` has four bespoke persisted values (groupCollapse, sidebarWidth,
// lastRepo, sessionOrder) but no generic persisted-boolean helper to route
// through, so the rail keeps its own two-line pair here rather than inventing a
// fifth shared abstraction on one caller's behalf. All access is wrapped: a
// blocked/unavailable localStorage degrades to "collapsed", never a crash.
function readRailExpanded(): boolean {
  try {
    return window.localStorage.getItem(RAIL_EXPANDED_KEY) === 'true'
  } catch {
    return false
  }
}

function writeRailExpanded(v: boolean): void {
  try {
    window.localStorage.setItem(RAIL_EXPANDED_KEY, String(v))
  } catch {
    /* ignore — persistence is best-effort */
  }
}

/** The theme picker's options. `setChoice` is fed an id straight off this list, so
 *  there is no second string-validating copy of `ThemeChoice` to keep in step. */
const THEME_CHOICES: { id: ThemeChoice; label: string; icon: LucideIcon }[] = [
  { id: 'system', label: 'System', icon: Monitor },
  { id: 'light', label: 'Light', icon: Sun },
  { id: 'dark', label: 'Dark', icon: Moon },
]

export function NavRail() {
  const { pathname } = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const badges = useNavBadges()
  const { width } = useViewport()
  const { choice, setChoice } = useTheme()

  const [expandedPref, setExpandedPref] = React.useState(readRailExpanded)
  // Expansion is offered only at the wide tier; the stored preference survives a
  // trip through a narrower window instead of being cleared by it.
  const canExpand = width === 'wide'
  const expanded = canExpand && expandedPref
  const toggleExpanded = () => {
    setExpandedPref((prev) => {
      writeRailExpanded(!prev)
      return !prev
    })
  }

  const themeRef = React.useRef<HTMLButtonElement>(null)
  const [themeOpen, setThemeOpen] = React.useState(false)
  const closeThemeMenu = React.useCallback(() => setThemeOpen(false), [])

  const activeId = navDefForPath(pathname)?.id
  const settings = NAV_BY_ID.settings

  /** Drawers are URL state, not local state, so they survive a reload and a share. */
  const openDrawer = (name: 'assistant' | 'help') => {
    const next = new URLSearchParams(searchParams)
    next.set('drawer', name)
    setSearchParams(next)
  }

  const badgeProps = (def: NavDef) => {
    const v = def.badge ? badges[def.badge] : undefined
    return typeof v === 'number' ? { count: v } : { attention: v === true }
  }

  return (
    <nav
      data-region="app.rail"
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
          {...badgeProps(def)}
        />
      ))}

      <div className="flex-1" />

      <NavItem to="/sessions?dialog=new" icon={Plus} label="New session" showLabel={expanded} />
      <NavItem onClick={() => openDrawer('assistant')} icon={Sparkles} label="Assistant" showLabel={expanded} />
      <NavItem onClick={() => openDrawer('help')} icon={HelpCircle} label="Help" showLabel={expanded} />
      <NavItem
        onClick={() => setThemeOpen((o) => !o)}
        buttonRef={themeRef}
        icon={THEME_CHOICES.find((t) => t.id === choice)?.icon ?? Monitor}
        label="Theme"
        showLabel={expanded}
      />
      <Menu
        open={themeOpen}
        onClose={closeThemeMenu}
        anchorRef={themeRef}
        label="Theme"
        items={THEME_CHOICES.map((t) => ({
          id: t.id,
          label: t.label,
          icon: <t.icon size={16} aria-hidden="true" />,
          selected: t.id === choice,
        }))}
        onSelect={(id) => {
          const picked = THEME_CHOICES.find((t) => t.id === id)
          if (picked) setChoice(picked.id)
        }}
      />
      <NavItem
        to={settings.path}
        icon={settings.icon}
        label={settings.label}
        showLabel={expanded}
        active={activeId === 'settings'}
        {...badgeProps(settings)}
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
