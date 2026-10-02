// One held issue, with the exact taps that release it. Fed by the escalation read
// model (`GET /pm/holds`), which lists every parked item for a product whether or
// not it sits under a capability — the capability-scoped `LeafList` cannot reach
// an unclassified issue, which is how ghostdriver-mcp#28 sat 12 days behind a
// "1 need a home" pseudo-row with no title, no reason and no Retry (2026-10-02).
//
// Every mutation here is keyed on THIS card's `workItemId`, never on a list index
// or the product: a card can only ever act on the issue it names.
import { useContext, useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'
import type { HeldItem } from '@/api/roadmap'
import { useApproveEscalation, useReleaseHold } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { holdExplanation, holdLabel, ROADMAP } from '@/features/products/vocab'
import { tierLabel } from '@/session/modelName'
import { sessionPath } from '@/session/sessionNavState'
import { inboxPath } from '../roadmapNavState'
import { IssueDispositionContext } from './IssueDispositionContext'
import { dispatchOutcomeText, heldFor } from './holdOutcome'

export function HeldIssueCard({ item, showProduct = false }: { item: HeldItem; showProduct?: boolean }) {
  const reviewIssue = useContext(IssueDispositionContext)
  // Hooks are unconditional; a row the mirror has not resolved (`workItemId`
  // null) simply renders no per-item taps — only the GitHub link.
  const itemId = item.workItemId ?? ''
  const release = useReleaseHold(itemId)
  const approve = useApproveEscalation(itemId)
  const [notice, setNotice] = useState<string | null>(null)
  const [spawned, setSpawned] = useState<{ id: string; name: string | null } | null>(null)
  const { why, next } = holdExplanation(item, tierLabel)
  const pending = release.isPending || approve.isPending
  const age = heldFor(item.ageHours)
  const retry = async () => {
    setNotice(null)
    try { await release.mutateAsync() } catch { /* shown inline below */ }
  }
  const approveEscalation = async () => {
    if (!item.escalation) return
    setNotice(null)
    setSpawned(null)
    try {
      const result = await approve.mutateAsync(item.escalation.needsApproval)
      const text = dispatchOutcomeText(result, result.model)
      if (text) setNotice(text)
      if (result.status === 'spawned' && result.session_id) {
        setSpawned({ id: result.session_id, name: result.session_name })
        toast.success(result.session_name ? `Started ${result.session_name}` : 'Started a session')
      }
    } catch { /* shown inline below */ }
  }
  const error = release.error ?? approve.error
  return (
    <li className="flex flex-col gap-2 rounded-md border border-edge bg-surface p-3 text-sm" data-testid="held-issue">
      <div className="flex flex-wrap items-center gap-2">
        {item.url
          ? <a href={item.url} target="_blank" rel="noreferrer" className="min-w-0 font-medium text-ink hover:underline">#{item.number} {item.title ?? item.ref}</a>
          : <span className="min-w-0 font-medium text-ink">#{item.number} {item.title ?? item.ref}</span>}
        <Badge tone="warning">{holdLabel(item.holdKind)}</Badge>
        {item.downstream > 0 && <Badge tone="danger">Blocks {item.downstream}</Badge>}
        <span className="text-xs text-ink-muted">
          {showProduct && <>{item.product}{' · '}</>}
          {item.repo}
          {age && <>{' · '}{age}</>}
        </span>
      </div>
      <p className="max-w-prose text-ink">{why}</p>
      {next && <p className="max-w-prose text-ink-muted">{next}</p>}
      <div className="flex flex-wrap items-center gap-2">
        {item.holdKind === 'owner_action' && itemId && (
          <Button size="sm" loading={release.isPending} disabled={pending} onClick={() => void retry()}>Retry</Button>
        )}
        {item.escalation && itemId && (
          <Button size="sm" variant="secondary" loading={approve.isPending} disabled={pending} onClick={() => void approveEscalation()}>
            Approve escalation to {tierLabel(item.escalation.needsApproval)}
          </Button>
        )}
        {item.holdKind === 'decision' && (
          <Link to={inboxPath({ proposal: item.proposalId })} className="text-accent hover:underline">
            Open in the {ROADMAP.inbox.toLowerCase()}
          </Link>
        )}
        {itemId && reviewIssue && (
          <Button size="sm" variant="ghost" disabled={pending} onClick={() => reviewIssue(itemId)}>Review and decide</Button>
        )}
        {item.url && <a href={item.url} target="_blank" rel="noreferrer" className="text-xs text-accent hover:underline">Edit on GitHub</a>}
      </div>
      {error && <p role="alert" className="text-danger">{extractMutationErrorMessage(error, 'Could not update this hold.')}</p>}
      {notice && <p role="status" className="text-warning">{notice}</p>}
      {spawned && <Link to={sessionPath(spawned.id)} className="text-accent hover:underline">Open session{spawned.name ? ` ${spawned.name}` : ''}</Link>}
    </li>
  )
}
