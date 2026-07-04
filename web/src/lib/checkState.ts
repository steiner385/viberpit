import type { NodeState } from '@/api/types'

export type CheckVisualState = 'done' | 'active' | 'failed' | 'pending' | 'overdue' | 'skipped' | 'queued'

// Bar/fill classes for drill-down check rows.
export function checkStateColor(state: CheckVisualState): string {
  switch (state) {
    case 'done': return 'bg-green-500'
    case 'active': return 'bg-accent'
    case 'failed': return 'bg-red-500'
    case 'overdue': return 'bg-amber-500'
    case 'skipped':
    case 'queued':
    case 'pending':
    default: return 'bg-gray-300 dark:bg-gray-600'
  }
}

// CVD-safe overlay applied on top of the fill color (see GanttBar). Plotroom values.
export function checkStateTexture(state: CheckVisualState): string {
  switch (state) {
    case 'overdue':
      return 'repeating-linear-gradient(90deg, transparent 0 4px, rgba(255,255,255,0.24) 4px 6px)'
    case 'failed':
      return 'repeating-linear-gradient(-45deg, rgba(255,255,255,0.30) 0 2px, transparent 2px 6px)'
    default:
      return ''
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
