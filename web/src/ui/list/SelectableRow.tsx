//
// A row with a selection checkbox in front of it.
//
// The shift-capture is the reason this is a component rather than a class
// constant: a checkbox's `change` event carries no modifier keys, but its
// `click` does, and click always fires first (even for a keyboard Space
// toggle). Both former copies re-derived that with their own ref.
import { useRef, type ReactNode } from 'react'
import { cn } from '@/lib/utils'

// eslint-disable-next-line react-refresh/only-export-components -- SELECT_CHECKBOX_CLS is a cn() call, which allowConstantExport does not exempt
export const SELECT_CHECKBOX_CLS = cn(
  'h-4 w-4 flex-shrink-0 rounded border-edge-strong text-accent',
  'focus:ring-accent bg-surface-raised',
  'checked:bg-accent cursor-pointer',
)

interface SelectableRowProps {
  id: string
  /** Row name — the checkbox is labelled "Select {name}". */
  name: string
  checked: boolean
  onToggle: (id: string, shift: boolean) => void
  children: ReactNode
}

export function SelectableRow({ id, name, checked, onToggle, children }: SelectableRowProps) {
  const shiftHeld = useRef(false)
  return (
    // `min-w-0 w-full`: as a flex child of a row (`ConsolidatePanel`'s and
    // `ManualDispatchDialog`'s `<li class="flex">`) a flex item cannot shrink
    // below its content without `min-w-0`, so a truncating link inside kept
    // its 500–590px and the phone's PAGE scrolled sideways by 275px (2026-10-01
    // Delivery re-critique, P1). In the block contexts the sessions lists use
    // it in, both classes are no-ops.
    <div className="flex min-w-0 w-full items-center gap-2">
      <input
        type="checkbox"
        checked={checked}
        onClick={(e) => { shiftHeld.current = e.shiftKey }}
        onChange={() => onToggle(id, shiftHeld.current)}
        aria-label={`Select ${name}`}
        className={SELECT_CHECKBOX_CLS}
      />
      <div className="flex-1 min-w-0">{children}</div>
    </div>
  )
}
