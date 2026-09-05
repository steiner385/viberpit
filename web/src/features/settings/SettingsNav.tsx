import { NavLink } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Badge } from '@/ui/Badge'
import { badgedName } from '@/ui/badgedName'
import { settingsSectionPath } from '@/shell/navDefs'
import { SETTINGS_GROUPS, sectionsInGroup, type SettingsDef } from './settingsDefs'
import { useSettingsBadges } from './useSettingsBadges'
import { SCOPE_LABEL } from './scope'

/**
 * Derived from settingsDefs. `rail`: the vertical nav beside the panel
 * (desktop / tablet-landscape). `list`: the full-width grouped list that IS
 * /settings on phone and tablet-portrait (iOS idiom, C18).
 *
 * One component rather than two because the two variants must never disagree
 * about WHICH sections exist or what they are called — the fourteen-tab strip
 * this replaces had exactly that problem, with its own hand-written label list
 * beside the route table's.
 */
export function SettingsNav({ variant }: { variant: 'rail' | 'list' }) {
  const badges = useSettingsBadges()
  const count = (d: SettingsDef) => (d.badge ? badges[d.badge] : undefined)
  const isList = variant === 'list'

  const body = SETTINGS_GROUPS.map((g) => (
    <li key={g.id} className="flex flex-col gap-0.5">
      <span className="px-2 pb-1 text-2xs font-semibold uppercase tracking-wider text-ink-muted">{g.label}</span>
      <ul className={cn('flex flex-col', isList ? 'gap-1' : 'gap-0.5')}>
        {sectionsInGroup(g.id).map((d) => (
          <li key={d.id}>
            <NavLink
              to={settingsSectionPath(d.id)}
              // The whole accessible name in ONE node: a label span next to a
              // "(2)" span is announced "Accounts(2)" (see ui/badgedName).
              aria-label={badgedName({ label: d.label, count: count(d) })}
              className={({ isActive }) => cn(
                'flex items-center gap-2 rounded-md px-2 text-sm text-ink min-h-target',
                'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                isList ? 'border border-edge bg-surface px-3' : 'hover:bg-surface-raised',
                // The rail is the only variant with a "current" state to show:
                // in the list variant the page IS the list, so nothing on it is
                // the open section.
                !isList && isActive && 'bg-accent-wash text-accent font-medium',
              )}
            >
              <d.icon size={16} aria-hidden="true" className="shrink-0" />
              <span className="flex-1 truncate">{d.label}</span>
              {count(d) !== undefined && <Badge tone="warning" count={count(d)} />}
              {isList && (
                <>
                  {d.scope !== 'server' && d.scope !== 'none' && (
                    <span className="text-xs text-ink-muted">{SCOPE_LABEL[d.scope]}</span>
                  )}
                  <ChevronRight size={16} aria-hidden="true" className="text-ink-faint" />
                </>
              )}
            </NavLink>
          </li>
        ))}
      </ul>
    </li>
  ))

  // The list variant is NOT a <nav>: it is the page's content, and the page
  // already sits inside the shell's `main`. A second navigation landmark for
  // what is really a list of links would make the phone page announce two.
  return isList
    ? <ul aria-label="Settings sections" className="flex flex-col gap-4">{body}</ul>
    : (
      <nav aria-label="Settings sections" className="w-60 shrink-0 overflow-y-auto border-r border-edge px-2 py-3">
        <ul className="flex flex-col gap-4">{body}</ul>
      </nav>
      )
}
