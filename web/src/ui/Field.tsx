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
  const describedBy = [helpId, errorId].filter(Boolean).join(' ') || undefined
  const control = React.cloneElement(children, { 'aria-describedby': describedBy, required })
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
