/**
 * MessageActions — the per-turn "⋯" menu (Desktop parity, sub-spec D §4). D-3 ships
 * Copy as Markdown only; later items (Fork, Rewind, Edit) append to `ACTIONS`.
 */
import { useRef, useState } from 'react'
import { MoreHorizontal } from 'lucide-react'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import { Menu } from '@/ui/Menu'
import type { Turn } from '@/session/reducer'
import { turnMarkdown } from '@/session/turnMarkdown'
import { copyText } from '@/lib/copyText'

interface MessageAction {
  id: string
  label: string
  run: (turn: Turn) => Promise<void>
  /** Hidden when false. */
  available: (turn: Turn) => boolean
}

const ACTIONS: readonly MessageAction[] = [
  {
    id: 'copy-markdown',
    label: 'Copy as Markdown',
    available: (turn) => turnMarkdown(turn) !== null,
    run: async (turn) => {
      const md = turnMarkdown(turn)
      if (md !== null && (await copyText(md))) toast('Copied')
      else toast.error("Couldn't copy")
    },
  },
]

/**
 * Reveal rule, scoped to THIS turn only. The trigger is a direct child of its turn's
 * root, so `[:hover>&]` / `[:focus-within>&]` match only the nearest turn. A
 * `group-hover/turn:` variant would not do: Tailwind compiles it to "any `.group/turn`
 * ancestor is hovered", which an outer turn always is for a nested sub-agent turn, so
 * hovering the parent would reveal every nested trigger.
 */
const REVEAL_ON_TURN_HOVER = '[:hover>&]:opacity-100 [:focus-within>&]:opacity-100'

export function MessageActions({ turn, className }: { turn: Turn; className?: string }) {
  const anchorRef = useRef<HTMLButtonElement>(null)
  const [open, setOpen] = useState(false)
  const actions = ACTIONS.filter((a) => a.available(turn))
  if (actions.length === 0) return null

  return (
    <>
      <button
        ref={anchorRef}
        type="button"
        aria-label={`Message actions — ${turn.role === 'user' ? 'your prompt' : 'reply'}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'inline-flex items-center justify-center rounded-md p-1 min-h-7 min-w-7 text-ink-muted',
          'opacity-0 focus-visible:opacity-100 aria-expanded:opacity-100 pointer-coarse:opacity-100',
          REVEAL_ON_TURN_HOVER,
          'hover:bg-edge active:bg-edge-strong',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
          className,
        )}
      >
        <MoreHorizontal size={14} aria-hidden="true" />
      </button>
      <Menu
        open={open}
        onClose={() => setOpen(false)}
        anchorRef={anchorRef}
        label="Message actions"
        align="end"
        items={actions.map((a) => ({ id: a.id, label: a.label }))}
        onSelect={(id) => { void actions.find((a) => a.id === id)?.run(turn) }}
      />
    </>
  )
}
