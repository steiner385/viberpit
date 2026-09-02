import * as React from 'react'
import { cn } from '@/lib/utils'

export interface FieldProps {
  label: string
  /** id of the control inside; Field derives `${htmlFor}-help` / `${htmlFor}-error`. */
  htmlFor: string
  help?: React.ReactNode
  error?: React.ReactNode
  required?: boolean
  className?: string
  children: React.ReactElement<{ 'aria-describedby'?: string; required?: boolean }>
}

export function Field({ label, htmlFor, help, error, required, className, children }: FieldProps) {
  const helpId = help ? `${htmlFor}-help` : undefined
  const errorId = error ? `${htmlFor}-error` : undefined
  // Preserve the child's own `aria-describedby` (a caller may already document
  // the control) and append Field's ids after it, so the caller's description
  // reads first. Only ever write keys Field actually has a value for —
  // cloneElement copies every own-enumerable key from the config verbatim,
  // `undefined` included, so an unconditional call here would silently wipe
  // out a child's existing `required` / `aria-describedby`.
  const childDescribedBy = children.props['aria-describedby']
  const describedBy = [childDescribedBy, helpId, errorId].filter(Boolean).join(' ') || undefined
  const config: { 'aria-describedby'?: string; required?: boolean } = {}
  if (describedBy) config['aria-describedby'] = describedBy
  if (required !== undefined) config.required = required
  const control = React.cloneElement(children, config)
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <label htmlFor={htmlFor} className="text-sm font-medium text-ink">
        {label}{required && <span aria-hidden="true" className="text-danger"> *</span>}
      </label>
      {control}
      {help && <p id={helpId} className="text-xs text-ink-muted">{help}</p>}
      {error && <p id={errorId} role="alert" className="text-xs text-danger">{error}</p>}
    </div>
  )
}
