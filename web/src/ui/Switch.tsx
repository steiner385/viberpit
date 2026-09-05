import * as React from 'react'
import { cn } from '@/lib/utils'

export interface SwitchProps {
  checked: boolean
  onChange: (next: boolean) => void
  id?: string
  disabled?: boolean
  'aria-label'?: string
  'aria-labelledby'?: string
  'aria-describedby'?: string
  className?: string
}

/**
 * On/off that applies immediately (spec §7.4). A `<button role="switch">`, not a
 * checkbox: a checkbox in a form means "save later", a switch means "done now".
 * The label is supplied by the caller and MUST NOT change with state (APG).
 * Space and Enter toggle; native button semantics give Enter for free and the
 * keydown handler adds Space without scrolling the page.
 */
export const Switch = React.forwardRef<HTMLButtonElement, SwitchProps>(function Switch(
  { checked, onChange, id, disabled, className, ...aria }, ref,
) {
  return (
    <button
      ref={ref}
      id={id}
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={aria['aria-label']}
      aria-labelledby={aria['aria-labelledby']}
      aria-describedby={aria['aria-describedby']}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      onKeyDown={(e) => {
        if (e.key === ' ') { e.preventDefault(); onChange(!checked) }
      }}
      className={cn(
        'relative inline-flex h-6 w-11 shrink-0 items-center rounded-pill border transition-colors',
        'focus:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        'disabled:opacity-50 disabled:cursor-not-allowed',
        checked ? 'bg-accent border-accent' : 'bg-surface-raised border-edge-strong',
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          'inline-block h-4 w-4 rounded-pill transition-transform',
          checked ? 'translate-x-6 bg-accent-ink' : 'translate-x-1 bg-ink-muted',
        )}
      />
    </button>
  )
})
