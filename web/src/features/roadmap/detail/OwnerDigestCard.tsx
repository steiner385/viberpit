// Today's owner digest (design 2026-10-14 §5) at the top of the Needs-you tab:
// every item that truly needs a person, grouped by product, each with at most ONE
// recommended action — approve a model climb, apply a hygiene recommendation
// (close, unpark, release …), accept a confident triage proposal — and "Approve
// all" behind a dialog that lists exactly what will happen. Items with no
// recommendation (a decision, a checklist, a stage gate) say why and link to
// where a person decides; a gate is never advanced from here.
import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { DigestApplyResult, DigestItem, ProductNode } from '@/api/roadmap'
import { useApplyDigest, useOwnerDigest } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Badge } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { Card } from '@/ui/Card'
import { ConfirmDialog } from '@/ui/ConfirmDialog'
import { SectionHeading } from '@/ui/SectionHeading'
import { DIGEST_KIND_LABEL } from './holdOutcome'

function ItemTitle({ item }: { item: DigestItem }) {
  const text = item.ref && !item.title.includes(item.ref) ? `${item.title} (${item.ref})` : item.title
  if (!item.url) return <span className="min-w-0 font-medium text-ink">{text}</span>
  if (item.url.startsWith('/')) {
    return <Link to={item.url} className="min-w-0 font-medium text-ink hover:underline">{text}</Link>
  }
  return <a href={item.url} target="_blank" rel="noreferrer" className="min-w-0 font-medium text-ink hover:underline">{text}</a>
}

function outcomeText(result: DigestApplyResult): string {
  const failed = result.results.filter((r) => !r.ok)
  const head = `Applied ${result.applied} of ${result.results.length}.`
  return failed.length === 0 ? head : `${head} Not applied: ${failed.map((r) => r.error ?? r.step ?? r.id).join('; ')}`
}

export function OwnerDigestCard({ products }: { products: ProductNode[] }) {
  const inScope = new Set(products.map((p) => p.slug))
  const { data } = useOwnerDigest(products.length === 1 ? products[0].slug : undefined)
  const apply = useApplyDigest()
  const [confirming, setConfirming] = useState(false)
  const [outcome, setOutcome] = useState<string | null>(null)
  const groups = (data?.products ?? []).filter((p) => inScope.has(p.slug) && p.items.length > 0)
  const items = groups.flatMap((g) => g.items)
  if (items.length === 0) return null
  const recommended = items.filter((i) => i.recommendation)
  const run = async (ids: string[]) => {
    setOutcome(null)
    try {
      setOutcome(outcomeText(await apply.mutateAsync(ids)))
    } catch { /* shown inline below */ }
  }

  return (
    <Card as="section" padding="sm" aria-label="Today's digest" className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-ink">Today's digest</h3>
          <p className="text-xs text-ink-muted">
            {items.length} {items.length === 1 ? 'item needs' : 'items need'} you — {recommended.length}{' '}
            {recommended.length === 1 ? 'has' : 'have'} a recommendation
          </p>
        </div>
        {recommended.length > 0 && (
          <Button size="sm" disabled={apply.isPending} onClick={() => setConfirming(true)}>
            Approve all recommendations ({recommended.length})
          </Button>
        )}
      </div>
      {groups.map((group) => (
        <div key={group.slug} className="flex flex-col gap-1">
          {groups.length > 1 && <SectionHeading>{group.slug}</SectionHeading>}
          <ul className="flex flex-col gap-2" aria-label={`Digest for ${group.slug}`}>
            {group.items.map((item) => (
              <li key={item.id} className="flex flex-col gap-1 rounded-md border border-edge p-2 text-sm" data-testid="digest-item">
                <div className="flex flex-wrap items-center gap-2">
                  <ItemTitle item={item} />
                  <Badge tone={item.recommendation ? 'accent' : 'warning'}>{DIGEST_KIND_LABEL[item.kind] ?? item.kind}</Badge>
                </div>
                {item.why && <p className="max-w-prose whitespace-pre-line text-ink-muted">{item.why}</p>}
                {item.evidence.length > 0 && (
                  <ul className="flex flex-wrap gap-x-3 text-xs" aria-label="Evidence">
                    {item.evidence.map((e) => (
                      <li key={e.url}><a href={e.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">{e.title}</a></li>
                    ))}
                  </ul>
                )}
                {item.recommendation ? (
                  <div>
                    <Button size="sm" variant="secondary" loading={apply.isPending && apply.variables?.length === 1 && apply.variables[0] === item.id}
                      disabled={apply.isPending} onClick={() => void run([item.id])}>
                      {item.recommendation.label}
                    </Button>
                  </div>
                ) : item.kind === 'gate' && item.url ? (
                  <Link to={item.url} className="text-xs text-accent hover:underline">Review the gate</Link>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      ))}
      {apply.error && <p role="alert" className="text-sm text-danger">{extractMutationErrorMessage(apply.error, 'Could not apply the digest.')}</p>}
      {outcome && <p role="status" className="text-sm text-ink-muted">{outcome}</p>}
      <ConfirmDialog
        open={confirming}
        onClose={() => setConfirming(false)}
        onConfirm={async () => {
          setConfirming(false)
          await run(recommended.map((i) => i.id))
        }}
        title={`Approve ${recommended.length} ${recommended.length === 1 ? 'recommendation' : 'recommendations'}?`}
        description={(
          <ul className="list-disc pl-5" aria-label="What will happen">
            {recommended.map((i) => <li key={i.id}>{i.recommendation?.label} — {i.title}</li>)}
          </ul>
        )}
        confirmLabel="Approve all"
        tone="warning"
        busy={apply.isPending}
      />
    </Card>
  )
}
