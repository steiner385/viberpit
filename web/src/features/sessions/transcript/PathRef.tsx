import { useEffect, useRef, useState } from 'react'
import { ChevronDown } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { copyText } from '@/lib/copyText'
import { Menu } from '@/ui/Menu'
import { ICON_TOUCH_FLOOR } from '@/ui/list/listControlStyles'
import { CopyablePath } from './CopyablePath'
import { parseFilePathRef, worktreeRelativePath } from './filePathRef'
import { useFileRefActions, type FileRefActions } from './fileRefContext'

/**
 * A path-like inline code span in the transcript (D-7). Under the live
 * session's `cwd` (see `fileRefContext`) it is a link to the Files tab at its
 * `:line`, with a small menu for Copy path and Attach as @; anything else — a
 * path outside the worktree, or no provider at all (archived reader, sub-agent
 * page, assistant drawer) — keeps the click-to-copy `CopyablePath`.
 */
export function PathRef({ text }: { text: string }) {
  const actions = useFileRefActions()
  const ref = parseFilePathRef(text)
  const rel = actions && ref ? worktreeRelativePath(ref.path, actions.cwd) : null
  if (!actions || !ref || rel === null) return <CopyablePath text={text} />
  return <FileLink text={text} path={ref.path} rel={rel} line={ref.line ?? null} actions={actions} />
}

function FileLink({ text, path, rel, line, actions }: {
  text: string
  /** The path as written, without its `:line[:col]` — what Copy path copies. */
  path: string
  rel: string
  line: number | null
  actions: FileRefActions
}) {
  const anchorRef = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const { attachMention } = actions
  const items = [
    { id: 'copy', label: 'Copy path' },
    ...(attachMention ? [{ id: 'attach', label: 'Attach as @' }] : []),
  ]
  // Attach runs once the menu has CLOSED, not on select: the menu hands focus
  // back to its trigger as it closes, which would otherwise race the composer
  // taking focus for the inserted mention. This effect runs after the menu's
  // own (a child's effects run first), so the composer's focus lands last.
  const pendingAttach = useRef<string | null>(null)
  useEffect(() => {
    if (open || pendingAttach.current === null) return
    const path = pendingAttach.current
    pendingAttach.current = null
    attachMention?.(path)
  }, [open, attachMention])
  const onSelect = async (id: string) => {
    if (id === 'attach') { pendingAttach.current = rel; return }
    if (await copyText(path)) toast('Copied')
    else toast.error("Couldn't copy")
  }

  return (
    <span className="inline-flex items-baseline">
      <button
        type="button"
        aria-label={`Open ${text} in Files`}
        title="Open in Files"
        onClick={() => actions.openFile(rel, line)}
        className="cursor-pointer rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        <code className="text-accent underline underline-offset-2 hover:bg-edge">{text}</code>
      </button>
      <button
        ref={anchorRef}
        type="button"
        aria-label={`More actions for ${text}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          // The 44px coarse-pointer floor, pulled out of the line box by the
          // negative margin so a phone's prose keeps its line height.
          ICON_TOUCH_FLOOR, '-my-3 [@media(hover:hover)]:my-0',
          'inline-flex items-center justify-center self-center rounded-sm text-ink-muted',
          'hover:bg-edge active:bg-edge-strong',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        )}
      >
        <ChevronDown size={12} aria-hidden="true" />
      </button>
      <Menu
        open={open}
        onClose={() => setOpen(false)}
        anchorRef={anchorRef}
        label={`Actions for ${text}`}
        items={items}
        onSelect={(id) => { void onSelect(id) }}
      />
    </span>
  )
}
