import * as React from 'react'
import { cn } from '@/lib/utils'

export interface RadioOption<V extends string> {
  value: V
  label: string
  description?: React.ReactNode
  disabled?: boolean
  /** Shown under the label when disabled — never hide an option, say why (C13). */
  disabledReason?: string
}

export interface RadioGroupProps<V extends string> {
  name: string
  legend: string
  /** Visually hide the legend when a SettingRow label already names the group. */
  hideLegend?: boolean
  value: V
  onChange: (next: V) => void
  options: RadioOption<V>[]
  'aria-describedby'?: string
  className?: string
}

/** 2–5 mutually exclusive values, all visible (spec §7.4). Native radios in a fieldset. */
export function RadioGroup<V extends string>({
  name, legend, hideLegend, value, onChange, options, className, ...aria
}: RadioGroupProps<V>) {
  return (
    <fieldset className={cn('flex flex-col gap-1.5', className)} aria-describedby={aria['aria-describedby']}>
      <legend className={cn('text-sm font-medium text-ink', hideLegend && 'sr-only')}>{legend}</legend>
      {options.map((o) => {
        const id = `${name}-${o.value}`
        const reasonId = o.disabled && o.disabledReason ? `${id}-reason` : undefined
        return (
          <div key={o.value} className="flex flex-col">
            <label
              htmlFor={id}
              className={cn(
                'inline-flex items-center gap-2 min-h-target text-sm text-ink',
                o.disabled ? 'opacity-60 cursor-not-allowed' : 'cursor-pointer',
              )}
            >
              <input
                id={id}
                type="radio"
                name={name}
                value={o.value}
                checked={o.value === value}
                disabled={o.disabled}
                aria-describedby={reasonId}
                onChange={() => onChange(o.value)}
                className="h-4 w-4 accent-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              />
              <span>{o.label}</span>
            </label>
            {o.description && <p className="pl-6 text-xs text-ink-muted">{o.description}</p>}
            {reasonId && <p id={reasonId} className="pl-6 text-xs text-ink-muted">{o.disabledReason}</p>}
          </div>
        )
      })}
    </fieldset>
  )
}
