import * as React from 'react'
import { cn } from '@/lib/utils'

export interface SettingRowProps {
  label: string
  htmlFor: string
  description?: React.ReactNode
  error?: React.ReactNode
  status?: React.ReactNode
  wide?: boolean
  children: React.ReactElement<{ 'aria-describedby'?: string }>
}

/**
 * Label · description · one control (Microsoft SettingsCard shape, C4). The
 * description is always visible — never tooltip-only (S24). The control's
 * `aria-describedby` is appended with the description and error ids, in that
 * order, preserving anything the caller already set (same rule as ui/Field).
 */
export function SettingRow({ label, htmlFor, description, error, status, wide = false, children }: SettingRowProps) {
  const descId = description ? `${htmlFor}-desc` : undefined
  const errId = error ? `${htmlFor}-error` : undefined
  const own = children.props['aria-describedby']
  const describedBy = [own, descId, errId].filter(Boolean).join(' ') || undefined
  const control = describedBy ? React.cloneElement(children, { 'aria-describedby': describedBy }) : children

  return (
    <div className={cn('grid gap-3 py-3', wide ? 'grid-cols-1' : 'sm:grid-cols-[minmax(0,1fr)_minmax(200px,42%)] sm:items-center')}>
      <div className="min-w-0">
        <label htmlFor={htmlFor} className="block text-sm font-medium text-ink">{label}</label>
        {description && <p id={descId} className="mt-0.5 text-xs text-ink-muted">{description}</p>}
        {error && <p id={errId} role="alert" className="mt-0.5 text-xs text-danger"><span className="sr-only">Error: </span>{error}</p>}
      </div>
      <div className={cn('flex items-center gap-2', !wide && 'sm:justify-end')}>
        {status !== undefined && status !== null && (
          <span role="status" aria-live="polite" className="text-xs text-success whitespace-nowrap">{status}</span>
        )}
        {control}
      </div>
    </div>
  )
}
