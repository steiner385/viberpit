// Product-to-product dependencies (roadmap spec §8 "Product dependency"): the
// ccswitch-owned edges, as chips with a remove each, and one row to add one.
// Distinct from `DependencyPanel` below it, which mirrors ISSUE-level blocked-by
// drift from GitHub; these edges are what the portfolio tiers and the
// per-product dependency tab read.
import { useState } from 'react'
import { X } from 'lucide-react'
import type { ProductNode } from '@/api/roadmap'
import { PROJECT_EDGE_KINDS, type EdgeKind } from '@/api/pm'
import { useCreateEdge, useDeleteEdge, useProject, useRoadmapMap } from '@/api/hooks'
import { Field } from '@/ui/Field'
import { Select } from '@/ui/Select'
import { Input } from '@/ui/Input'
import { Button } from '@/ui/Button'
import { IconButton } from '@/ui/IconButton'
import { Skeleton } from '@/ui/Skeleton'
import { PRODUCT } from '@/features/products/vocab'

const KIND_LABEL: Record<EdgeKind, string> = {
  depends_on: 'depends on', deploys_via: 'deploys via', integrates_with: 'integrates with',
}

export function ProductEdgesEditor({ product }: { product: ProductNode }) {
  const { data: project, isLoading } = useProject(product.slug)
  const { data: map } = useRoadmapMap({ poll: false })
  const create = useCreateEdge(product.slug)
  const remove = useDeleteEdge(product.slug)
  const [kind, setKind] = useState<EdgeKind>('depends_on')
  const [dst, setDst] = useState('')
  const [note, setNote] = useState('')
  const edges = project?.edges ?? []
  const taken = new Set(edges.map((e) => `${e.kind}:${e.dstSlug}`))
  const nameOf = (slug: string | null) => map?.products.find((p) => p.slug === slug)?.name ?? slug ?? '?'
  // Every other product on the map that this one does not already point at with this kind.
  const targets = (map?.products ?? []).filter((p) => p.slug !== product.slug && !taken.has(`${kind}:${p.slug}`))
  const canAdd = dst !== '' && !create.isPending

  if (isLoading && !project) return <Skeleton lines={2} />
  return (
    <div className="flex flex-col gap-3">
      {edges.length === 0
        ? <p className="text-sm text-ink-muted">No {PRODUCT.singular.toLowerCase()} dependencies yet.</p>
        : (
          <ul aria-label={`${PRODUCT.singular} dependencies`} className="flex flex-wrap gap-2">
            {edges.map((e) => {
              const label = `${KIND_LABEL[e.kind as EdgeKind] ?? e.kind} ${nameOf(e.dstSlug)}`
              return (
                <li key={e.id} className="flex items-center gap-1 rounded-pill border border-edge bg-surface-raised py-0.5 pl-3 pr-1 text-sm text-ink" title={e.note ?? undefined}>
                  <span>{label}</span>
                  <IconButton size="sm" aria-label={`Remove: ${label}`} disabled={remove.isPending} onClick={() => remove.mutate(e.id)}>
                    <X size={14} aria-hidden="true" />
                  </IconButton>
                </li>
              )
            })}
          </ul>
        )}
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => { e.preventDefault(); if (canAdd) create.mutate({ dstSlug: dst, kind, ...(note.trim() ? { note: note.trim() } : {}) }, { onSuccess: () => { setDst(''); setNote('') } }) }}
      >
        <Field label="Relationship" htmlFor="edge-kind">
          <Select id="edge-kind" value={kind} onChange={(e) => { setKind(e.target.value as EdgeKind); setDst('') }}>
            {PROJECT_EDGE_KINDS.map((k) => <option key={k} value={k}>{KIND_LABEL[k]}</option>)}
          </Select>
        </Field>
        <Field label={PRODUCT.singular} htmlFor="edge-dst">
          <Select id="edge-dst" value={dst} onChange={(e) => setDst(e.target.value)}>
            <option value="">— choose —</option>
            {targets.map((p) => <option key={p.slug} value={p.slug}>{p.name ?? p.slug}</option>)}
          </Select>
        </Field>
        <Field label="Note" htmlFor="edge-note">
          <Input id="edge-note" value={note} onChange={(e) => setNote(e.target.value)} placeholder="optional" />
        </Field>
        <Button type="submit" disabled={!canAdd} loading={create.isPending}>Add</Button>
      </form>
    </div>
  )
}
