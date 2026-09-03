import * as React from 'react'
import { cn } from '@/lib/utils'
import { controlClass } from './Input'
import { autoGrow } from '@/lib/autoGrow'

export interface TextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  invalid?: boolean
  /** Grow to fit content on input (no scrollbar until max-height). */
  autoGrow?: boolean
  /** Cap for autoGrow, in px. Defaults to the shared helper's own default. */
  maxHeightPx?: number
}

export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, invalid, autoGrow: shouldAutoGrow = false, maxHeightPx, onInput, ...props }, ref) => {
    const handleInput = (e: React.InputEvent<HTMLTextAreaElement>) => {
      if (shouldAutoGrow) {
        autoGrow(e.currentTarget, maxHeightPx)
      }
      onInput?.(e)
    }
    return (
      <textarea
        ref={ref}
        aria-invalid={invalid || undefined}
        onInput={handleInput}
        className={cn(
          controlClass,
          'px-3 py-2 text-sm leading-relaxed resize-y',
          shouldAutoGrow && 'resize-none overflow-hidden',
          className,
        )}
        {...props}
      />
    )
  },
)
Textarea.displayName = 'Textarea'
