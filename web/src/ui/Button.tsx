import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { Loader2 } from 'lucide-react'
import { cn } from '@/lib/utils'

// Same API shape as the old components/ui/button.tsx; only the classes and
// variant/size names changed — every color is a token so light/dark flip at
// :root (no dark: variants).
const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 rounded-md text-sm font-medium transition-colors ' +
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-canvas ' +
  'disabled:pointer-events-none disabled:opacity-50',
  {
    variants: {
      variant: {
        primary: 'bg-accent text-accent-ink hover:bg-accent/90',
        secondary: 'border border-edge bg-surface text-ink hover:bg-surface-raised',
        ghost: 'text-ink hover:bg-surface-raised',
        // -strong is the theme-invariant fill: white text needs 4.5:1, and
        // --t-danger only clears that in light mode (dark mode drops to
        // 3.18:1). See web/src/index.css for the token contract.
        danger: 'bg-danger-strong text-white hover:bg-danger-strong/90',
      },
      size: {
        sm: 'h-9 px-3',
        md: 'h-10 px-4 py-2',
        lg: 'h-11 min-h-target px-6',
      },
    },
    defaultVariants: { variant: 'primary', size: 'md' },
  },
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  /** Disables the button and shows a spinner in place of iconLeft. */
  loading?: boolean
  iconLeft?: React.ReactNode
  iconRight?: React.ReactNode
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, loading = false, iconLeft, iconRight, disabled, children, ...props }, ref) => (
    <button
      ref={ref}
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading
        ? <Loader2 size={16} aria-hidden="true" data-spinner className="animate-spin motion-reduce:animate-none" />
        : iconLeft}
      {children}
      {iconRight}
    </button>
  ),
)
Button.displayName = 'Button'

// eslint-disable-next-line react-refresh/only-export-components
export { Button, buttonVariants }
