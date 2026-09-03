import { shortcutsByGroup, type Shortcut } from '@/lib/shortcuts'
import { Kbd } from '@/ui/Kbd'
import { Sheet } from '@/ui/Sheet'

const NAV_MAP = [
  { name: 'Sessions', detail: 'Live sessions list — the default view' },
  { name: 'Accounts', detail: 'Manage Claude accounts and rotation' },
  { name: 'Archived', detail: 'Review, unarchive, or permanently delete past sessions' },
  { name: 'Session view', detail: 'Open a session to read its transcript and send prompts' },
  { name: 'Review', detail: 'Read-only transcript of an archived session' },
]

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
  {
    title: 'Permission modes',
    detail: 'Inside a session, Shift+Tab cycles the permission mode.',
  },
]

export function HelpDrawer({ onClose }: { onClose: () => void }) {
  return (
    <Sheet open onClose={onClose} title="Help & Shortcuts" side="right">
      <div className="space-y-6">
        <ShortcutSection title="Compose mode" bindings={shortcutsByGroup('compose')} />
        <ShortcutSection title="List-focus mode" bindings={shortcutsByGroup('list-mode')} />
        <ShortcutSection title="Group headers" bindings={shortcutsByGroup('list-mode-header')} />
        <ShortcutSection title="In-session" bindings={shortcutsByGroup('in-session')} />
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted mb-2">Navigation</h3>
          <dl className="space-y-1">
            {NAV_MAP.map((n) => (
              <div key={n.name} className="flex items-baseline gap-3">
                <dt className="shrink-0 min-w-28 text-sm font-medium text-ink">{n.name}</dt>
                <dd className="text-sm text-ink-muted">{n.detail}</dd>
              </div>
            ))}
          </dl>
        </section>
        <section>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted mb-2">Tips</h3>
          <dl className="space-y-2">
            {TIPS.map((t) => (
              <div key={t.title}>
                <dt className="text-sm font-medium text-ink">{t.title}</dt>
                <dd className="text-sm text-ink-muted">{t.detail}</dd>
              </div>
            ))}
          </dl>
        </section>
      </div>
    </Sheet>
  )
}

function ShortcutSection({ title, bindings }: { title: string; bindings: Shortcut[] }) {
  return (
    <section>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-muted mb-2">
        {title}
      </h3>
      <dl className="space-y-1">
        {bindings.map((b) => (
          <div key={b.id} className="flex items-baseline gap-3">
            <dt className="shrink-0 min-w-[120px]">
              <Kbd keys={b.keys} />
            </dt>
            <dd className="text-sm text-ink">{b.description}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
