import * as React from 'react'
import { HelpCircle, Monitor, Moon, Sparkles, Sun, type LucideIcon } from 'lucide-react'
import { Menu } from '@/ui/Menu'
import { useOpenDrawer } from '@/shell/drawerParam'
import { useTheme, type ThemeChoice } from '@/shell/theme'
import { NavItem } from './NavItem'

/**
 * The shell's global actions — the controls that belong to the app rather than
 * to a section: Assistant, Help, Theme.
 *
 * They appear in `NavRail`'s bottom cluster on tablet/desktop and in `NavBar`'s
 * More sheet on phone. This is the same reason `NavItem` exists: the list, the
 * order and the theme options are one thing, and a second copy would drift the
 * first time an action was added. Settings is deliberately NOT here — it is a
 * nav section with an active state, so each region renders it itself, once.
 *
 * "New session" is NOT here either, and that is a decision rather than an
 * omission. It used to lead the cluster, as a `<Link>` to `?dialog=new`, and it
 * read as a duplicate of the Sessions list's own New Session button while
 * actually doing something else — the link opened a modal create form, the list
 * button flips `app.dock` to the `DispatchBar`. Creating a session is now the
 * dock's job alone, so the entry point lives at the foot of the list column,
 * against the bar it operates (`SessionListColumn`). A global action here would
 * put a create control in Pipeline, Insights and Settings, where there is no
 * dock for it to reach.
 *
 * `showLabel` is passed straight through to `NavItem`; the accessible name is
 * present either way.
 */

/** The theme picker's options. `setChoice` is fed an id straight off this list, so
 *  there is no second string-validating copy of `ThemeChoice` to keep in step. */
const THEME_CHOICES: { id: ThemeChoice; label: string; icon: LucideIcon }[] = [
  { id: 'system', label: 'System', icon: Monitor },
  { id: 'light', label: 'Light', icon: Sun },
  { id: 'dark', label: 'Dark', icon: Moon },
]

export function GlobalActions({ showLabel = false }: { showLabel?: boolean }) {
  const openDrawer = useOpenDrawer()
  const { choice, setChoice } = useTheme()

  const themeRef = React.useRef<HTMLButtonElement>(null)
  const [themeOpen, setThemeOpen] = React.useState(false)
  const closeThemeMenu = React.useCallback(() => setThemeOpen(false), [])

  return (
    <>
      <NavItem onClick={() => openDrawer('assistant')} icon={Sparkles} label="Assistant" showLabel={showLabel} />
      <NavItem onClick={() => openDrawer('help')} icon={HelpCircle} label="Help" showLabel={showLabel} />
      <NavItem
        onClick={() => setThemeOpen((o) => !o)}
        buttonRef={themeRef}
        icon={THEME_CHOICES.find((t) => t.id === choice)?.icon ?? Monitor}
        label="Theme"
        showLabel={showLabel}
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
    </>
  )
}
