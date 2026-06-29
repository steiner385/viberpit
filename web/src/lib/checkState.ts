import type { NodeState } from '@/api/types'

export type CheckVisualState = 'done' | 'active' | 'failed' | 'pending' | 'overdue'

// Bar/fill classes for drill-down check rows.
export function checkStateColor(state: CheckVisualState): string {
  switch (state) {
    case 'done': return 'bg-green-500'
    case 'active': return 'bg-accent'
    case 'failed': return 'bg-red-500'
    case 'overdue': return 'bg-amber-500'
    case 'pending':
    default: return 'bg-gray-300 dark:bg-gray-600'
  }
}

// Circle classes for stage-track nodes (moved verbatim from StageTrack).
export function nodeCircleClass(state: NodeState | 'pending'): string {
  switch (state) {
    case 'done':
      return 'bg-green-500 border-green-500 text-white dark:bg-green-500 dark:border-green-500'
    case 'active':
      return 'bg-accent border-accent text-accent-foreground ring-2 ring-accent/40'
    case 'failed':
      return 'bg-red-500 border-red-500 text-white dark:bg-red-500 dark:border-red-500'
    case 'pending':
    default:
      return 'bg-transparent border-gray-300 dark:border-gray-600 text-gray-400 dark:text-gray-500'
  }
}
