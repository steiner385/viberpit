// web/src/features/sessions/composer/MentionMenu.tsx
import { forwardRef, useImperativeHandle, useMemo, useState, useEffect, useId } from 'react'
import { cn } from '@/lib/utils'
import { TOUCH_FLOOR } from '@/ui/list/listControlStyles'

export interface MentionSource {
  id: string
  title: string
}

export interface MentionMenuHandle {
  /** Returns true if the key was consumed by the menu. */
  handleKeyDown: (e: React.KeyboardEvent) => boolean
}

interface Props {
  sessions: MentionSource[]
  /** Worktree file paths, already ranked by the server (D-7). Absent = no Files
   *  group at all (the assistant drawer, or an engine with no cwd). */
  files?: string[]
  /** The first file results are still in flight — the Files group shows a loading row. */
  filesLoading?: boolean
  /** The file rows answer an earlier query (the debounce or the fetch for the typed
   *  one has not caught up): they show dimmed, and Enter/Tab wait rather than pick. */
  filesStale?: boolean
  query: string
  onSelect: (session: MentionSource) => void
  onSelectFile?: (path: string) => void
  onDismiss: () => void
  /** The highlighted option's DOM id (undefined when none) — the textarea, which
   *  keeps focus, mirrors it as `aria-activedescendant`. */
  onActiveChange?: (id: string | undefined) => void
  /** The listbox's DOM id, owned by the textarea for `aria-controls`. */
  listboxId?: string
  /** Whether the listbox is actually rendered (it hides with nothing to show) — the
   *  textarea's `aria-expanded`. */
  onVisibleChange?: (visible: boolean) => void
}

const MAX_PER_GROUP = 8

type Item =
  | { kind: 'file'; key: string; label: string; path: string }
  | { kind: 'session'; key: string; label: string; session: MentionSource }

export const MentionMenu = forwardRef<MentionMenuHandle, Props>(function MentionMenu(
  { sessions, files, filesLoading = false, filesStale = false, query, onSelect, onSelectFile, onDismiss, onActiveChange, listboxId, onVisibleChange }, ref,
) {
  const grouped = files !== undefined || filesLoading
  const fileItems = useMemo<Item[]>(
    () => (files ?? []).slice(0, MAX_PER_GROUP).map((path) => ({ kind: 'file', key: `f:${path}`, label: path, path })),
    [files],
  )
  const sessionItems = useMemo<Item[]>(() => {
    const q = query.toLowerCase()
    return sessions
      .filter((s) => s.title.toLowerCase().includes(q))
      .slice(0, MAX_PER_GROUP)
      .map((s) => ({ kind: 'session', key: `s:${s.id}`, label: s.title, session: s }))
  }, [sessions, query])
  // One flat order for the keyboard: Files first, then Sessions.
  const items = useMemo(() => [...fileItems, ...sessionItems], [fileItems, sessionItems])

  // The highlight is held by item KEY, not index: file results arriving after the user
  // moved it (debounced, async) must not shift it onto a different item. A new query
  // re-highlights the top; a highlighted item that drops out falls back to the top.
  const [selKey, setSelKey] = useState<string | null>(null)
  useEffect(() => { setSelKey(null) }, [query])
  const found = selKey === null ? -1 : items.findIndex((it) => it.key === selKey)
  const active = items.length === 0 ? -1 : Math.max(found, 0)
  const visible = items.length > 0 || filesLoading
  useEffect(() => { onVisibleChange?.(visible) }, [visible, onVisibleChange])
  useEffect(() => () => onVisibleChange?.(false), [onVisibleChange])

  const baseId = useId()
  const optionId = (i: number) => `${baseId}-opt-${i}`
  const activeId = active >= 0 ? optionId(active) : undefined
  useEffect(() => { onActiveChange?.(activeId) }, [activeId, onActiveChange])
  useEffect(() => () => onActiveChange?.(undefined), [onActiveChange])

  useImperativeHandle(ref, () => {
    const pick = (item: Item) => {
      if (item.kind === 'file') onSelectFile?.(item.path)
      else onSelect(item.session)
    }
    return {
      handleKeyDown(e: React.KeyboardEvent): boolean {
        if (e.key === 'Escape') { e.preventDefault(); onDismiss(); return true }
        const selects = e.key === 'Enter' || (e.key === 'Tab' && !e.shiftKey)
        // Files still loading, or the rows on screen answer an older query: the menu
        // is up, so Enter/Tab must neither fall through and send the half-typed
        // `@query` nor insert a file that the typed query never matched.
        const waiting = filesLoading || (filesStale && fileItems.length > 0)
        if (waiting && selects) { e.preventDefault(); return true }
        if (items.length === 0) return false
        if (e.key === 'ArrowDown') { e.preventDefault(); setSelKey(items[(active + 1) % items.length].key); return true }
        if (e.key === 'ArrowUp')   { e.preventDefault(); setSelKey(items[(active - 1 + items.length) % items.length].key); return true }
        if (selects) { e.preventDefault(); pick(items[active]); return true }
        return false
      },
    }
  }, [items, fileItems.length, active, filesLoading, filesStale, onSelect, onSelectFile, onDismiss])

  if (!visible) return null

  const renderOption = (item: Item, i: number) => (
    <button
      key={item.key}
      id={optionId(i)}
      type="button"
      role="option"
      aria-selected={i === active}
      onMouseEnter={() => setSelKey(item.key)}
      onClick={() => (item.kind === 'file' ? onSelectFile?.(item.path) : onSelect(item.session))}
      className={cn(
        'block w-full truncate px-3 py-2 text-left text-sm',
        TOUCH_FLOOR,
        item.kind === 'file' && 'font-mono text-xs',
        item.kind === 'file' && filesStale && 'text-ink-muted',
        i === active ? 'bg-accent-wash' : 'hover:bg-surface-raised',
      )}
    >
      {item.label}
    </button>
  )

  const groupLabel = (id: string, text: string) => (
    <div id={id} className="px-3 pt-2 pb-1 text-xs font-medium text-ink-muted">{text}</div>
  )

  return (
    <div
      role="listbox"
      id={listboxId}
      aria-label={grouped ? 'File and session mentions' : 'Session mentions'}
      className={cn(
        'max-sm:fixed max-sm:inset-x-0 max-sm:bottom-0 max-sm:rounded-b-none max-sm:rounded-t-xl',
        'sm:absolute sm:bottom-full sm:left-0 sm:right-0 sm:mb-1',
        'max-h-64 overflow-y-auto z-popover',
        'border border-edge bg-surface shadow-lg',
        'rounded-md',
      )}
    >
      {grouped ? (
        <>
          {(fileItems.length > 0 || filesLoading) && (
            <div role="group" aria-labelledby={`${baseId}-files`} aria-busy={filesLoading || filesStale || undefined}>
              {groupLabel(`${baseId}-files`, 'Files')}
              {fileItems.length === 0 && (
                <div
                  role="option"
                  aria-disabled="true"
                  aria-selected={false}
                  aria-live="polite"
                  className={cn('px-3 py-2 text-sm text-ink-muted', TOUCH_FLOOR)}
                >
                  Searching files…
                </div>
              )}
              {fileItems.map((item, i) => renderOption(item, i))}
            </div>
          )}
          {sessionItems.length > 0 && (
            <div role="group" aria-labelledby={`${baseId}-sessions`}>
              {groupLabel(`${baseId}-sessions`, 'Sessions')}
              {sessionItems.map((item, i) => renderOption(item, fileItems.length + i))}
            </div>
          )}
        </>
      ) : (
        sessionItems.map((item, i) => renderOption(item, i))
      )}
    </div>
  )
})
