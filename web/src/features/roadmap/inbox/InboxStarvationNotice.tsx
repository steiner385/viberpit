import { useHolds } from '@/api/hooks'
import { Banner } from '@/ui/Banner'

const MAX_LINES = 3

const line = (p: { slug: string; dispatchable: number; heldCount: number }) =>
  `${p.slug}: ${p.dispatchable} dispatchable, ${p.heldCount} held — the runner is waiting on you`

/** Spec 2026-09-30 §8: when a product's runner is starving (nothing dispatchable while
 *  items are held), say so above the list — ONE banner (one live region), the first
 *  product's line as its title and up to two more below, then `+N more`. Reads the
 *  escalation read model's per-product summary. */
export function InboxStarvationNotice({ product }: { product: string | null }) {
  const { data } = useHolds(product ?? undefined, { enabled: true })
  const starving = (data?.products ?? []).filter((p) => p.starving)
  if (starving.length === 0) return null
  const [first, ...rest] = starving.slice(0, MAX_LINES)
  const more = starving.length - MAX_LINES
  return (
    <Banner tone="warning" title={line(first)}>
      {rest.map((p) => <p key={p.slug}>{line(p)}</p>)}
      {more > 0 && <p>+{more} more</p>}
    </Banner>
  )
}
