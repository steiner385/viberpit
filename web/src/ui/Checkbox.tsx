import { cn } from '@/lib/utils'

export interface CheckboxProps {
  id: string
  label: string
  checked: boolean
  onChange: (next: boolean) => void
  disabled?: boolean
  'aria-describedby'?: string
  className?: string
}

/** On/off inside a SAVED form (spec §7.4). For immediate on/off use `Switch`. */
export function Checkbox({ id, label, checked, onChange, disabled, className, ...aria }: CheckboxProps) {
  return (
    <label
      htmlFor={id}
      className={cn(
        'inline-flex items-center gap-2 min-h-target text-sm text-ink cursor-pointer',
        disabled && 'opacity-50 cursor-not-allowed',
        className,
      )}
    >
      <input
        id={id}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-describedby={aria['aria-describedby']}
        onChange={(e) => onChange(e.target.checked)}
        className="h-4 w-4 rounded-sm border-edge-strong accent-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      />
      <span>{label}</span>
    </label>
  )
}
