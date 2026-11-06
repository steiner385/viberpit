import * as React from 'react'
import { Search } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Dialog } from '@/ui/Dialog'
import { Kbd } from '@/ui/Kbd'
import { SHORTCUTS } from '@/shell/shortcuts'
import type { PaletteHost } from './paletteHost'
import { paletteSections, usePaletteEntries, type PaletteEntry } from './usePaletteEntries'

/** The palette's own chord, read from its registry row so ⌘K / Ctrl+K inside
 *  the palette closes it with exactly the binding that opened it. */
const PALETTE_ROW = SHORTCUTS.find((s) => s.id === 'app.palette')

/**
 * `AppPalette` — the app's ⌘K / Ctrl+K palette (D-10). Not `CommandPalette`:
 * that name is the composer's slash-command menu.
 *
 * Lazy: `shell/regions/Overlays` imports this module only through `import()`,
 * on the first open, so neither it nor the archived/repos fetches it makes are
 * on the entry path.
 *
 * The input keeps focus throughout (a combobox driving a listbox through
 * `aria-activedescendant`), so ↑/↓/Enter never move focus and typing always
 * types. `ui/Dialog` supplies the modal, the focus trap, Esc and the focus
 * restore; `layer="palette"` lifts it over every other dialog.
 */
export function AppPalette({ host }: { host: PaletteHost }) {
  const [query, setQuery] = React.useState('')
  const prepared = usePaletteEntries(host, query)
  const [active, setActive] = React.useState(0)
  const inputRef = React.useRef<HTMLInputElement>(null)
  const listId = React.useId()

  const sections = React.useMemo(() => paletteSections(prepared, query), [prepared, query])
  const flat = React.useMemo(() => sections.flatMap((s) => s.entries), [sections])
  const current = flat.length === 0 ? -1 : Math.min(active, flat.length - 1)
  const optionId = (e: PaletteEntry) => `${listId}-${e.key}`
  const activeEntry = current >= 0 ? flat[current] : undefined

  // Keep the selected row in view as ↑/↓ walk past the list's edge.
  // Keyed on the entry's KEY, not its object: a re-derived list hands back new
  // entry objects for the same selection, which is no reason to scroll.
  const activeKey = activeEntry?.key
  React.useEffect(() => {
    if (activeKey === undefined) return
    document.getElementById(`${listId}-${activeKey}`)?.scrollIntoView?.({ block: 'nearest' })
  }, [activeKey, listId])

  const run = (e: PaletteEntry) => {
    if (e.drill !== undefined) {
      setQuery(e.drill)
      setActive(0)
      inputRef.current?.focus()
      return
    }
    host.close(e.run)
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (PALETTE_ROW?.match?.(e.nativeEvent)) { e.preventDefault(); host.close(); return }
    if (flat.length === 0) return
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((current + 1) % flat.length); return }
    if (e.key === 'ArrowUp') { e.preventDefault(); setActive((current - 1 + flat.length) % flat.length); return }
    if (e.key === 'Enter' && activeEntry) { e.preventDefault(); run(activeEntry) }
  }

  return (
    <Dialog
      open
      onClose={() => host.close()}
      aria-label="Command palette"
      size="lg"
      layer="palette"
      initialFocus={inputRef}
      className="p-0 space-y-0 overflow-hidden flex flex-col sm:self-start sm:mt-[12vh]"
      bodyClassName="flex min-h-0 flex-col"
    >
      <div className="flex items-center gap-2 border-b border-edge px-3">
        <Search aria-hidden="true" className="size-4 shrink-0 text-ink-faint" />
        <input
          ref={inputRef}
          role="combobox"
          aria-label="Search sessions, actions and settings"
          aria-expanded="true"
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={activeEntry ? optionId(activeEntry) : undefined}
          placeholder="Search sessions, actions, settings…"
          autoComplete="off"
          spellCheck={false}
          value={query}
          onChange={(e) => { setQuery(e.target.value); setActive(0) }}
          onKeyDown={onKeyDown}
          className="h-12 min-w-0 flex-1 bg-transparent text-sm text-ink focus:outline-none"
        />
      </div>
      <div id={listId} role="listbox" aria-label="Results" className="max-h-[60vh] overflow-y-auto p-1">
        {sections.length === 0 && (
          <p className="px-2 py-6 text-center text-sm text-ink-muted">No matches</p>
        )}
        {sections.map((s, k) => {
          const headingId = `${listId}-h-${s.id}`
          return (
            <div key={s.id} role="group" aria-labelledby={headingId} className={cn(k > 0 && 'mt-1 border-t border-edge pt-1')}>
              <div id={headingId} role="presentation" className="px-2 py-1 text-2xs font-medium uppercase tracking-wide text-ink-faint">
                {s.label}
              </div>
              {s.entries.map((e) => {
                const selected = e === activeEntry
                return (
                  <div
                    key={e.key}
                    id={optionId(e)}
                    role="option"
                    aria-selected={selected}
                    onMouseDown={(ev) => ev.preventDefault()}
                    onMouseMove={() => { if (!selected) setActive(flat.indexOf(e)) }}
                    onClick={() => run(e)}
                    className={cn(
                      'flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm text-ink cursor-default select-none min-h-target',
                      selected && 'bg-surface-raised',
                    )}
                  >
                    <span className="min-w-0 flex-1">
                      <span className="block truncate">{e.label}</span>
                      {e.detail && <span className="block truncate text-xs text-ink-muted">{e.detail}</span>}
                    </span>
                    {e.chord && <span className="hidden shrink-0 sm:inline"><Kbd keys={e.chord} /></span>}
                  </div>
                )
              })}
            </div>
          )
        })}
      </div>
    </Dialog>
  )
}
