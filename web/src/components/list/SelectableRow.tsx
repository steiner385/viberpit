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
  'h-4 w-4 flex-shrink-0 rounded border-gray-300 text-blue-600',
  'focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700',
  'dark:checked:bg-blue-500 cursor-pointer',
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
    <div className="flex items-center gap-2">
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
