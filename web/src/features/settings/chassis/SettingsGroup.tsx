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
      /* `min-w-0`: a fieldset's default `min-inline-size` is `min-content`, so a
         single long unbreakable line (a commit subject, an env value) widened
         the whole group past the viewport — on a phone the Deploy button sat
         at x=676 in a 390px screen and the Limits pane scrolled sideways
         (2026-09-30 Settings critique, P0). A `min-w-0` on the children alone
         cannot fix it; it has to be on the fieldset. */
      className={cn('min-w-0 rounded-lg border bg-surface px-4 pb-2 pt-1', tone === 'danger' ? 'border-danger' : 'border-edge', className)}
    >
      <legend className={cn('px-1 text-xs font-semibold uppercase tracking-wide', tone === 'danger' ? 'text-danger' : 'text-ink-muted')}>
        {legend}
      </legend>
      {description && <p className="mb-1 text-xs text-ink-muted">{description}</p>}
      <div className="divide-y divide-edge">{children}</div>
    </fieldset>
  )
}
