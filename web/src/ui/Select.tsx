import * as React from 'react'
import { cn } from '@/lib/utils'
import { controlClass } from './Input'

const SIZES = { sm: 'h-9 px-2 text-sm', md: 'h-10 min-h-target px-3 text-sm' } as const

export interface SelectProps extends Omit<React.SelectHTMLAttributes<HTMLSelectElement>, 'size'> {
  invalid?: boolean
  size?: keyof typeof SIZES
}

export const Select = React.forwardRef<HTMLSelectElement, SelectProps>(
  ({ className, invalid, size = 'md', ...props }, ref) => (
    <select ref={ref} aria-invalid={invalid || undefined} className={cn(controlClass, SIZES[size], 'pr-8', className)} {...props} />
  ),
)
Select.displayName = 'Select'
