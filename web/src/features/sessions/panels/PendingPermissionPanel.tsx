import type { ComponentProps } from 'react'
import type { Turn, Block } from '@/session/reducer'
import { cn } from '@/lib/utils'
import { PermissionCard } from './PermissionCard'

type PermissionBlock = Extract<Block, { kind: 'permission' }>

interface Props {
  turns: Turn[]
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  /** Standing-rule wiring, passed straight to the card (see PermissionCard). */
  hasRepo?: boolean
  onPreviewRule?: ComponentProps<typeof PermissionCard>['onPreviewRule']
  onGrantRule?: ComponentProps<typeof PermissionCard>['onGrantRule']
}

/** First open permission across all turns (one-at-a-time pinning). */
function findFirstOpenPermission(turns: Turn[]): PermissionBlock | null {
  for (const t of turns) {
    for (const b of t.blocks) {
      if (b.kind === 'permission') return b
    }
  }
  return null
}

/**
 * Pins the live permission request above the composer so its allow/deny controls
 * are always visible — mirroring PendingQuestionPanel. Without this, the only
 * actionable PermissionCard rendered inline in the (scrollable) transcript, so a
 * pending permission below the fold left the user with a "Permission needed" status
 * and no reachable buttons.
 */
export function PendingPermissionPanel({ turns, onDecision, hasRepo, onPreviewRule, onGrantRule }: Props) {
  const perm = findFirstOpenPermission(turns)
  if (!perm) return null
  return (
    <div
      role="region"
      aria-label="Pending permission"
      aria-live="polite"
      className={cn(
        'flex-shrink-0 border-t border-warning',
        'bg-warning-wash/60 px-3 py-2',
        // Same guard as PendingQuestionPanel: a long permission (tool details /
        // diff preview + allow/deny/always) can overflow this flex-shrink-0 panel
        // on a short viewport, pushing the decision buttons below the fold with no
        // reachable scroll. Cap and scroll internally.
        'max-h-[50vh] overflow-y-auto overscroll-contain',
      )}
      data-testid="pending-permission-panel"
    >
      <PermissionCard
        // Keyed so an open chooser cannot retarget to the next request when this one resolves.
        key={perm.permId}
        block={perm}
        onDecision={onDecision}
        hasRepo={hasRepo}
        onPreviewRule={onPreviewRule}
        onGrantRule={onGrantRule}
      />
    </div>
  )
}
