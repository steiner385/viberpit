// The product page's open map-changes PR (spec §9, R8c-3): its number and age,
// Rebase when the branch has fallen behind the default, a conflict banner when
// it can no longer merge cleanly (the roadmap never force-pushes over that —
// GitHub is where it gets resolved), and Abandon on every variant.
import { useState, type ReactNode } from 'react'
import type { ProductNode } from '@/api/roadmap'
import { useAbandonMapChanges, useRebaseMapChanges } from '@/api/hooks'
import { Banner, type BannerTone } from '@/ui/Banner'
import { Button } from '@/ui/Button'
import { ConfirmDialog } from '@/ui/ConfirmDialog'
import { relTime, isValidTimestamp } from '@/lib/relativeTime'
import { openMapChanges } from '../lookup'

export function MapChangesCard({ product }: { product: ProductNode }) {
  // "Is there a live PR?" is one reading, shared with the capability page's
  // pending note (`openMapChanges`).
  const mapChanges = openMapChanges(product)
  const abandon = useAbandonMapChanges(product.slug)
  const rebase = useRebaseMapChanges(product.slug)
  const [confirming, setConfirming] = useState(false)

  if (!mapChanges) return null

  const { prNumber, prUrl, behind, conflicting, openedAt } = mapChanges

  // A pointer can be open before its PR number is known (the row is written as
  // the branch is created, the number arrives with the PR). "PR #null" is worse
  // than saying it without a number, and a link to it is not offerable at all.
  const prLabel = prNumber != null ? `PR #${prNumber}` : 'the map changes PR'
  const prLink = prUrl && prNumber != null
    ? <a href={prUrl} target="_blank" rel="noreferrer" className="underline">{prLabel}</a>
    : <>{prLabel}</>
  const opened = isValidTimestamp(openedAt) ? `opened ${relTime(openedAt)}` : null

  const abandonButton = (
    <Button key="abandon" type="button" variant="ghost" size="sm" className="text-danger" onClick={() => setConfirming(true)}>
      Abandon
    </Button>
  )

  // Spec §9 asks ONE card to show the open PR, its age, and Rebase when behind
  // — so the PR line below is the same on every variant, and `behind` /
  // `conflicting` change only the tone, the title and what is offered. Before
  // this, being behind hid the very link the user needed to go look at the PR.
  let tone: BannerTone
  let title: ReactNode
  let note: ReactNode = null
  let actions: ReactNode

  if (conflicting) {
    tone = 'danger'
    title = 'Capability map changes conflict with the default branch'
    note = 'Resolve the conflict on GitHub; the roadmap will not force-push.'
    // No Rebase here — a conflicting branch cannot be fast-forwarded, and
    // offering the button would just trade one failure for another.
    actions = abandonButton
  } else if (behind) {
    tone = 'warning'
    title = 'Capability map changes are behind'
    actions = (
      <>
        <Button type="button" variant="secondary" size="sm" loading={rebase.isPending} onClick={() => rebase.mutate()}>
          Rebase
        </Button>
        {abandonButton}
      </>
    )
  } else {
    tone = 'info'
    title = 'Capability map changes are open'
    actions = abandonButton
  }

  return (
    <>
      {/* Persistent state, not a one-off event — role is passed explicitly so it
          reads "status" even for the danger tone, whose Banner default is the
          more assertive "alert". */}
      <Banner tone={tone} role="status" title={title} actions={actions}>
        {note && <p>{note}</p>}
        <p>{prLink}{opened && <> · {opened}</>}</p>
      </Banner>
      <ConfirmDialog
        open={confirming}
        onClose={() => setConfirming(false)}
        onConfirm={() => abandon.mutate(undefined, { onSuccess: () => setConfirming(false) })}
        title="Abandon map changes?"
        description={`Closes ${prLabel}, deletes the branch and removes every capability that only exists there. Type the product slug to confirm.`}
        confirmLabel="Abandon"
        tone="danger"
        typed={product.slug}
        busy={abandon.isPending}
      />
    </>
  )
}
