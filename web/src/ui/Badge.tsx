import * as React from 'react'
import { cn } from '@/lib/utils'

export type Tone = 'success' | 'warning' | 'danger' | 'info' | 'neutral' | 'accent'

const TONE_TEXT: Record<Tone, string> = {
  success: 'text-success', warning: 'text-warning', danger: 'text-danger', info: 'text-info', neutral: 'text-ink-muted', accent: 'text-accent',
}
const TONE_WASH: Record<Tone, string> = {
  success: 'bg-success-wash', warning: 'bg-warning-wash', danger: 'bg-danger-wash', info: 'bg-info-wash', neutral: 'bg-neutral-wash', accent: 'bg-accent-wash',
}
const TONE_FILL: Record<Tone, string> = {
  success: 'bg-success', warning: 'bg-warning', danger: 'bg-danger', info: 'bg-info', neutral: 'bg-neutral', accent: 'bg-accent',
}

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: Tone
  /** Numeric pill (tabular). */
  count?: number
  /** 8px dot only (e.g. "needs attention"). */
  dot?: boolean
}

function Badge({ tone = 'neutral', count, dot, className, children, ...props }: BadgeProps) {
  if (dot) return <span aria-hidden="true" className={cn('inline-block w-2 h-2 rounded-pill', TONE_FILL[tone], className)} {...props} />
  return (
    <span className={cn('inline-flex items-center rounded-pill px-1.5 py-0.5 text-xs font-medium leading-none tabular-nums', TONE_WASH[tone], TONE_TEXT[tone], className)} {...props}>
      {count ?? children}
    </span>
  )
}

export { Badge, TONE_TEXT, TONE_WASH, TONE_FILL }
