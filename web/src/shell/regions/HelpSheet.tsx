import type { ReactNode } from 'react'
import { shortcutsByGroup, type Shortcut, type ShortcutGroup } from '@/lib/shortcuts'
import { Kbd } from '@/ui/Kbd'
import { NAV_DEFS } from '@/shell/navDefs'

/**
 * The body of `?drawer=help` — every keyboard binding, every section of the app,
 * and the handful of concepts that are not obvious from the UI.
 *
 * ## Nothing here is hand-maintained
 *
 * It replaces `components/HelpDrawer.tsx`, whose content was *regenerated*
 * rather than copied. That file kept a literal `NAV_MAP` of five sections
 * ("Sessions / Accounts / Archived / Session view / Review") while the app had
 * eight and none of those last two were sections at all — the audit filed it as
 * F1.3. The fix is not a corrected literal, which would be stale again at the
 * next section: the Navigation list below maps `NAV_DEFS`, and `NavDef.help` is
 * a REQUIRED field, so a new section that ships without help text does not
 * compile. Shortcut rows come from `SHORTCUTS` the same way, which is why
 * `Shift+Tab` is absent from this file — it was removed from the registry (D8)
 * along with its handler, not filtered out here.
 *
 * ## It is a body, not a sheet
 *
 * `DrawerHost` owns the one `Sheet`; this renders inside it. A `Sheet` of its
 * own would make two drawers possible at once, which is the shape sub-spec B
 * §4.2 exists to prevent ("ONE `Sheet`, content by `?drawer=`").
 */

/** Shortcut groups in the order they are taught: what you can do at the prompt,
 *  then in the list, then inside a session. */
const GROUPS: { group: ShortcutGroup; title: string }[] = [
  { group: 'compose', title: 'Compose mode' },
  { group: 'list-mode', title: 'List-focus mode' },
  { group: 'list-mode-header', title: 'Group headers' },
  { group: 'in-session', title: 'In-session' },
]

/**
 * Concepts, not keys. Anything key-shaped belongs in `lib/shortcuts.ts` so it is
 * documented once, in the registry that also implements it.
 */
const TIPS = [
  {
    title: 'List-focus mode',
    detail:
      'Press Tab on an empty prompt to focus the list; then c/r/a/d act on the row. Esc returns to the prompt.',
  },
  {
    title: 'Accounts = config dirs',
    detail:
      'Each account is an isolated CLAUDE_CONFIG_DIR; ccswitch rotates them as usage allows.',
  },
  {
    title: 'Archive vs delete',
    detail: 'Archive (a) hides a session but keeps it; delete (d) removes it permanently.',
  },
]

export function HelpSheet() {
  return (
    <div className="space-y-6">
      {GROUPS.map((g) => (
        <ShortcutSection key={g.group} title={g.title} bindings={shortcutsByGroup(g.group)} />
      ))}
      <Section title="Navigation">
        <dl className="space-y-1">
          {NAV_DEFS.map((def) => (
            <div key={def.id} className="flex items-baseline gap-3">
              <dt className="shrink-0 min-w-28 text-sm font-medium text-ink">{def.label}</dt>
              <dd className="text-sm text-ink-muted">{def.help}</dd>
            </div>
          ))}
        </dl>
      </Section>
      <Section title="Tips">
        <dl className="space-y-2">
          {TIPS.map((t) => (
            <div key={t.title}>
              <dt className="text-sm font-medium text-ink">{t.title}</dt>
              <dd className="text-sm text-ink-muted">{t.detail}</dd>
            </div>
          ))}
        </dl>
      </Section>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted mb-2">{title}</h3>
      {children}
    </section>
  )
}

function ShortcutSection({ title, bindings }: { title: string; bindings: Shortcut[] }) {
  return (
    <Section title={title}>
      <dl className="space-y-1">
        {bindings.map((b) => (
          <div key={b.id} className="flex items-baseline gap-3">
            <dt className="shrink-0 min-w-32">
              <Kbd keys={b.keys} />
            </dt>
            <dd className="text-sm text-ink">{b.description}</dd>
          </div>
        ))}
      </dl>
    </Section>
  )
}
