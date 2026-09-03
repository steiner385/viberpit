import * as React from 'react'
import { cn } from '@/lib/utils'

/** Shared look for native controls; exported for one-off <input>s that can't use the component. */
export const controlClass =
  'w-full rounded-md border border-edge bg-surface text-ink placeholder:text-ink-faint ' +
  'focus:outline-none focus:ring-2 focus:ring-accent focus:border-accent ' +
  'disabled:opacity-50 disabled:cursor-not-allowed aria-invalid:border-danger aria-invalid:focus:ring-danger'

const SIZES = { sm: 'h-9 px-2.5 text-sm', md: 'h-10 min-h-target px-3 text-sm' } as const

export interface InputProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'size'> {
  invalid?: boolean
  size?: keyof typeof SIZES
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, invalid, size = 'md', ...props }, ref) => (
    <input ref={ref} aria-invalid={invalid || undefined} className={cn(controlClass, SIZES[size], className)} {...props} />
  ),
)
Input.displayName = 'Input'
