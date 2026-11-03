/**
 * `/rewind`'s picker (D-9): a menu over the dock composer of the session's recent
 * prompts, newest first. Picking one runs Rewind to here (its own confirm follows).
 *
 * Not lazy, unlike the confirm dialog: it is a thin wrapper over the shared `Menu`,
 * already on the critical path (the message menu), and a lazy chunk importing `Menu`
 * splits it out of its chunk — measured larger than rendering it here.
 */
import { useState } from 'react'
import { Menu } from '@/ui/Menu'
import type { Turn } from '@/session/reducer'

/** One prompt to pick, with its text. */
export interface PickerItem { turn: Turn; text: string }

export function RewindPicker({ items, onPick, onClose }: {
  items: readonly PickerItem[]; onPick: (turn: Turn) => void; onClose: () => void
}) {
  // Anchored to the shell's dock — `/rewind` is only ever typed there. Read once, at
  // mount, before the menu measures it.
  const [anchorRef] = useState(() => ({ current: document.querySelector<HTMLElement>('[data-region="app.dock"]') }))
  return (
    <Menu
      open
      onClose={onClose}
      anchorRef={anchorRef}
      label="Rewind to a message"
      className="max-w-xl"
      items={items.map(({ turn, text }) => ({ id: turn.id, label: <span className="block max-w-lg truncate">{text}</span> }))}
      onSelect={(id) => { const it = items.find((i) => i.turn.id === id); if (it) onPick(it.turn) }}
    />
  )
}
