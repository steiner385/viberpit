import * as React from 'react'
import { cn } from '@/lib/utils'

export interface SettingsGroupProps {
  legend: string
  description?: React.ReactNode
  tone?: 'default' | 'danger'
  children: React.ReactNode
  className?: string
}

/** A fieldset of related rows (C14). `tone="danger"` is the section's Danger zone, always last. */
export function SettingsGroup({ legend, description, tone = 'default', children, className }: SettingsGroupProps) {
  return (
    <fieldset
      data-tone={tone}
      className={cn('rounded-lg border bg-surface px-4 pb-2 pt-1', tone === 'danger' ? 'border-danger' : 'border-edge', className)}
    >
      <legend className={cn('px-1 text-xs font-semibold uppercase tracking-wide', tone === 'danger' ? 'text-danger' : 'text-ink-muted')}>
        {legend}
      </legend>
      {description && <p className="mb-1 text-xs text-ink-muted">{description}</p>}
      <div className="divide-y divide-edge">{children}</div>
    </fieldset>
  )
}
