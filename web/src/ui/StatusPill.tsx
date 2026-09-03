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
}

export function StatusPill({ label, Icon, tone, pulse, className }: StatusPillProps) {
  return (
    <span
      aria-label={`Status: ${label}`}
      className={cn(
        'inline-flex items-center gap-1 rounded-pill px-2 py-0.5 text-xs font-medium',
        TONE_TEXT[tone], TONE_WASH[tone],
        pulse && 'motion-safe:animate-pulse',
        className,
      )}
    >
      <Icon size={12} aria-hidden="true" />
      <span>{label}</span>
    </span>
  )
}
