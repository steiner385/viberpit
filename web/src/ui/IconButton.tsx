import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const iconButtonVariants = cva(
  'inline-flex items-center justify-center rounded-md min-h-target min-w-target transition-colors ' +
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        ghost: 'text-ink-muted hover:text-ink hover:bg-surface-raised',
        secondary: 'border border-edge bg-surface text-ink hover:bg-surface-raised',
      },
      size: { sm: 'h-9 w-9', md: 'h-10 w-10' },
    },
    defaultVariants: { variant: 'ghost', size: 'md' },
  },
)

export interface IconButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label'>,
    VariantProps<typeof iconButtonVariants> {
  /** Required: an icon-only control has no visible text. */
  'aria-label': string
}

export const IconButton = React.forwardRef<HTMLButtonElement, IconButtonProps>(
  ({ className, variant, size, type = 'button', ...props }, ref) => (
    <button ref={ref} type={type} className={cn(iconButtonVariants({ variant, size }), className)} {...props} />
  ),
)
IconButton.displayName = 'IconButton'
