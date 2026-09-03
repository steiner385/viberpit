import * as React from 'react'
import { cn } from '@/lib/utils'
import { TONE_FILL, type Tone } from './Badge'

const PAD = { none: '', sm: 'p-3', md: 'p-4' } as const

export interface CardProps extends React.HTMLAttributes<HTMLElement> {
  /** Left status rail. */
  tone?: Tone
  /** Hover/focus affordance for clickable cards. */
  interactive?: boolean
  padding?: keyof typeof PAD
  as?: 'div' | 'section' | 'article' | 'li'
}

export const Card = React.forwardRef<HTMLElement, CardProps>(
  ({ tone, interactive, padding = 'md', as: Tag = 'div', className, children, ...props }, ref) => {
    const Comp = Tag as React.ElementType
    return (
      <Comp
        ref={ref}
        data-tone={tone}
        className={cn(
          'relative rounded-lg border border-edge bg-surface text-ink',
          tone && 'pl-5',
          PAD[padding],
          interactive && 'transition-colors hover:border-edge-strong focus-within:ring-2 focus-within:ring-accent',
          className,
        )}
        {...props}
      >
        {tone && <span aria-hidden="true" className={cn('absolute left-0 top-2 bottom-2 w-1 rounded-r-sm', TONE_FILL[tone])} />}
        {children}
      </Comp>
    )
  },
)
Card.displayName = 'Card'
