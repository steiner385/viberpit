import { useHolds } from '@/api/hooks'
import { Banner } from '@/ui/Banner'

const MAX_LINES = 3

/** Spec 2026-09-30 §8: when a product's runner is starving (nothing dispatchable while
 *  items are held), say so above the list — one line per product, the shape of the
 *  hygiene notice (#2540). Reads the escalation read model's per-product summary. */
export function InboxStarvationNotice({ product }: { product: string | null }) {
  const { data } = useHolds(product ?? undefined, { enabled: true })
  const starving = (data?.products ?? []).filter((p) => p.starving).slice(0, MAX_LINES)
  if (starving.length === 0) return null
  return (
    <div className="flex flex-col gap-1">
      {starving.map((p) => (
        <Banner key={p.slug} tone="warning"
                title={`${p.slug}: ${p.dispatchable} dispatchable, ${p.heldCount} held — the runner is waiting on you`} />
      ))}
    </div>
  )
}
