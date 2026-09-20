import type { ComponentType } from 'react'
import { cn } from '@/lib/utils'
import { TONE_TEXT, TONE_WASH, type Tone } from './Badge'

export interface StatusPillProps {
  label: string
  Icon: ComponentType<{ size?: number; className?: string }>
  tone: Tone
  /** Action-required tiers animate (motion-safe). */
  pulse?: boolean
  className?: string
  /** Drop the text label below the `narrow:` breakpoint, leaving the toned icon
   *  alone — for a caller sitting in a row that competes for space on a narrow
   *  phone (issue #1400). The accessible name (`aria-label` below) and a native
   *  `title` tooltip both keep carrying the full label regardless. Off by
   *  default: a pill in a `flex-wrap` row (e.g. AccountsManager's badge row)
   *  already sheds width by wrapping and loses nothing by opting out. */
  hideLabelNarrow?: boolean
}

export function StatusPill({ label, Icon, tone, pulse, className, hideLabelNarrow }: StatusPillProps) {
  return (
    <span
      aria-label={`Status: ${label}`}
      title={hideLabelNarrow ? label : undefined}
      className={cn(
        'inline-flex items-center gap-1 rounded-pill px-2 py-0.5 text-xs font-medium',
        TONE_TEXT[tone], TONE_WASH[tone],
        pulse && 'motion-safe:animate-pulse',
        className,
      )}
    >
      <Icon size={12} aria-hidden="true" />
      <span className={hideLabelNarrow ? 'narrow:hidden' : undefined}>{label}</span>
    </span>
  )
}
