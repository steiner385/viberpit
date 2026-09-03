import { cn } from '@/lib/utils'

export interface SkeletonProps {
  lines?: number
  className?: string
}

/** Decorative loading placeholder. aria-hidden so screen readers announce
 *  loading state via PageContent's role="status" region, not a pile of empty
 *  boxes; respects prefers-reduced-motion via motion-reduce:animate-none. */
export function Skeleton({ lines = 3, className }: SkeletonProps) {
  return (
    <div aria-hidden="true" className={cn('space-y-2', className)}>
      {Array.from({ length: lines }, (_, i) => (
        <div
          key={i}
          className="h-4 rounded-sm bg-surface-raised animate-pulse motion-reduce:animate-none"
          style={{ width: `${100 - (i % 3) * 12}%` }}
        />
      ))}
    </div>
  )
}
