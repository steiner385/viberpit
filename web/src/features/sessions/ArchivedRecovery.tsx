/**
 * The archived reader's "where did this session's work go" notice (#3333). Lazy-loaded
 * by `ArchivedReviewPage`, which the route table imports eagerly: keeping the recovery
 * query and its rendering in their own chunk keeps them off the first-paint bundle.
 */
import { useSessionRecoveryRefs } from '@/api/worktreeHooks'
import { RecoveryRefs } from './rail/RecoveryRefs'

export default function ArchivedRecovery({ sessionId }: { sessionId: string }) {
  const recovery = useSessionRecoveryRefs(sessionId).data
  // Optional-chained: a payload without `recoveryRefs` must not crash the reader.
  if (!recovery || (recovery.recoveryRefs?.length ?? 0) === 0) return null
  return (
    <div className="border-b border-edge px-4 py-3 md:px-6" data-testid="archived-recovery">
      <p className="mb-1 text-sm text-ink">
        This session's worktree was discarded{recovery.branch ? ` (branch ${recovery.branch})` : ''}; its work is kept in git.
      </p>
      <RecoveryRefs refs={recovery.recoveryRefs} restore={recovery.restoreCommand} />
    </div>
  )
}
