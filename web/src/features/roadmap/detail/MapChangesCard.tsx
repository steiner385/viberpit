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

export function MapChangesCard({ product }: { product: ProductNode }) {
  const { mapChanges } = product
  const abandon = useAbandonMapChanges(product.slug)
  const rebase = useRebaseMapChanges(product.slug)
  const [confirming, setConfirming] = useState(false)

  // Only an OPEN pointer is a live PR to show; merged/abandoned pointers are
  // history the map already reflects.
  if (!mapChanges || mapChanges.status !== 'open') return null

  const { prNumber, prUrl, behind, conflicting, openedAt } = mapChanges

  const prLink = prUrl
    ? <a href={prUrl} target="_blank" rel="noreferrer" className="underline">PR #{prNumber}</a>
    : <>PR #{prNumber}</>

  const abandonButton = (
    <Button key="abandon" type="button" variant="ghost" size="sm" className="text-danger" onClick={() => setConfirming(true)}>
      Abandon
    </Button>
  )

  let tone: BannerTone
  let title: ReactNode
  let body: ReactNode = null
  let actions: ReactNode

  if (conflicting) {
    tone = 'danger'
    title = 'Capability map changes conflict with the default branch'
    body = (
      <>
        Resolve the conflict on GitHub; the roadmap will not force-push. {prLink}
      </>
    )
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
    title = <>Capability map changes open in {prLink}</>
    body = isValidTimestamp(openedAt) ? `opened ${relTime(openedAt)}` : null
    actions = abandonButton
  }

  return (
    <>
      {/* Persistent state, not a one-off event — role is passed explicitly so it
          reads "status" even for the danger tone, whose Banner default is the
          more assertive "alert". */}
      <Banner tone={tone} role="status" title={title} actions={actions}>
        {body}
      </Banner>
      <ConfirmDialog
        open={confirming}
        onClose={() => setConfirming(false)}
        onConfirm={() => abandon.mutate(undefined, { onSuccess: () => setConfirming(false) })}
        title="Abandon map changes?"
        description={`Closes PR #${prNumber}, deletes the branch and removes every capability that only exists there. Type the product slug to confirm.`}
        confirmLabel="Abandon"
        tone="danger"
        typed={product.slug}
        busy={abandon.isPending}
      />
    </>
  )
}
