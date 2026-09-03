import { cn } from '@/lib/utils'

export interface UsageRingProps {
  /** 0..1 fraction of usage. */
  value: number
  label: string
  size?: number
  className?: string
}

/** Context/plan usage ring (Claude Code Desktop's "usage ring"). value is 0..1. */
export function UsageRing({ value, label, size = 18, className }: UsageRingProps) {
  const v = Math.max(0, Math.min(1, value))
  const r = (size - 3) / 2
  const c = 2 * Math.PI * r
  const tone = v >= 0.9 ? 'stroke-danger' : v >= 0.7 ? 'stroke-warning' : 'stroke-accent'
  return (
    <svg role="img" aria-label={label} width={size} height={size} viewBox={`0 0 ${size} ${size}`} className={cn('shrink-0', className)}>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth={3} className="stroke-edge" />
      <circle
        cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth={3} strokeLinecap="round"
        strokeDasharray={`${c * v} ${c}`} transform={`rotate(-90 ${size / 2} ${size / 2})`} className={tone}
      />
    </svg>
  )
}
