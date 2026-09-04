/**
 * Triage review list (PM product layer, phase 2): a functional-but-plain flat
 * list of pending TriageProposals with Accept/Reject buttons. Deliberately
 * minimal — the keyboard-first inbox (j/k/a/e/r/x) is phase 3, which REPLACES
 * this component (design §8). No keyboard shortcuts here.
 */
import { useAcceptProposal, useRejectProposal, useTriage } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import type { TriageProposal } from '@/api/triage'

function Scores({ p }: { p: TriageProposal }) {
  if (p.reach == null && p.impact == null && p.effort == null && p.confidence == null) {
    return <span className="pm-muted">no scores</span>
  }
  return (
    <span className="pm-scores">
      R{p.reach ?? '—'} I{p.impact ?? '—'} E{p.effort ?? '—'} C{p.confidence ?? '—'}
    </span>
  )
}

function ProposalRow({ p }: { p: TriageProposal }) {
  const accept = useAcceptProposal()
  const reject = useRejectProposal()
  const busy = accept.isPending || reject.isPending
  // One alert per row: whichever action failed most recently wins (react-query
  // resets the other's state on the next mutate call, so they rarely overlap).
  const errorText = accept.isError
    ? extractMutationErrorMessage(accept.error, 'Could not accept proposal')
    : reject.isError
      ? extractMutationErrorMessage(reject.error, 'Could not reject proposal')
      : null
  return (
    <li className="pm-triage-row">
      <div className="pm-triage-main">
        <span className="pm-triage-title">
          {p.url ? (
            <a href={p.url} target="_blank" rel="noreferrer">{p.title}</a>
          ) : p.title}
        </span>
        <span className="pm-muted">
          {p.repo}#{p.number} · {p.kind}
        </span>
      </div>
      <div className="pm-triage-meta">
        {p.proposedFeatureSlug ? (
          <span className="pm-feature-chip">{p.proposedFeatureSlug}</span>
        ) : (
          <span className="pm-nofit-chip">no fit → {p.noFitSuggestion ?? '?'}</span>
        )}
        <Scores p={p} />
        <span className={`pm-conf pm-conf-${p.confidenceLevel ?? 'low'}`}>
          {p.confidenceLevel ?? 'low'}
        </span>
      </div>
      {p.rationale && <p className="pm-triage-rationale">{p.rationale}</p>}
      {errorText && <p role="alert" className="pm-error">{errorText}</p>}
      <div className="pm-triage-actions">
        <button disabled={busy} onClick={() => accept.mutate({ id: p.id })}>Accept</button>
        <button disabled={busy} onClick={() => reject.mutate({ id: p.id })}>Reject</button>
      </div>
    </li>
  )
}

export function TriageReviewList({ projectSlug }: { projectSlug?: string }) {
  const { data, isLoading } = useTriage(projectSlug)
  if (isLoading) return <p className="pm-muted">Loading triage…</p>
  const items = data ?? []
  return (
    <section className="pm-triage">
      <h3>Triage queue{items.length ? ` (${items.length})` : ''}</h3>
      {items.length === 0 ? (
        <p className="pm-muted">Nothing to triage.</p>
      ) : (
        <ul className="pm-triage-list">
          {items.map((p) => <ProposalRow key={p.id} p={p} />)}
        </ul>
      )}
    </section>
  )
}
