// One row of the Map scope's tree: a product, a capability, or one of the two
// pseudo rows a product hangs off itself (awaiting triage / needs a home).
// Initiatives get their own card — they carry a stage chip and a reason.
//
// The frame is `CardFrame`, the same one the sessions list uses, so a roadmap
// row and a session row share their rail, selection ring and open control.
// Tokens only — this file is under `src/features/`.
import { ChevronDown, ChevronRight, HelpCircle, Inbox, Layers, Package } from 'lucide-react'
import { cn } from '@/lib/utils'
import { CardFrame } from '@/components/card/CardFrame'
import { Badge } from '@/ui/Badge'
import { badgedName } from '@/ui/badgedName'
import { CAPABILITY, PRODUCT } from '@/features/products/vocab'
import { TIER_ACCENT, TIER_TONE, tierLabel } from '../roadmapTier'
import type { TreeRow } from '../treeRows'
import { indentClass } from './indent'
import { TierDot } from './TierDot'

export interface TreeRowCardProps {
  row: Exclude<TreeRow, { kind: 'initiative' }>
  selected: boolean
  onOpen: (row: TreeRow) => void
  onToggle: (key: string) => void
  onOpenInbox: (productSlug: string) => void
}

export function TreeRowCard({ row, selected, onOpen, onToggle, onOpenInbox }: TreeRowCardProps) {
  if (row.kind === 'pseudo') {
    const label = row.pseudo === 'awaiting-triage' ? 'Awaiting triage' : 'Needs a home'
    const Icon = row.pseudo === 'awaiting-triage' ? Inbox : HelpCircle
    return (
      <div
        data-testid={`row-${row.key}`}
        role="treeitem"
        aria-level={row.depth + 1}
        className={indentClass(row.depth)}
      >
        <CardFrame
          id={row.key}
          openLabel={`Open ${label.toLowerCase()} for ${row.product.name ?? row.product.slug}`}
          railClass={TIER_ACCENT['needs-you'].rail}
          selected={selected}
          listMode={false}
          focusable
          swipeEnabled={false}
          onOpen={() => onOpenInbox(row.product.slug)}
          leading={<span aria-hidden="true" className="size-6 shrink-0" />}
          identity={
            <span className="flex min-w-0 items-center gap-2 text-sm text-ink">
              <Icon size={14} aria-hidden="true" className="shrink-0 text-ink-muted" />
              <span className="truncate">{label}</span>
              <Badge tone="warning" count={row.count} />
            </span>
          }
        />
      </div>
    )
  }

  const isProduct = row.kind === 'product'
  const title = isProduct
    ? (row.product.name ?? row.product.slug)
    : (row.capability.title ?? row.capability.slug)
  const tier = isProduct ? row.product.tier : row.capability.tier
  const noun = isProduct ? PRODUCT.singular : CAPABILITY.singular
  const meta = isProduct
    ? `${row.product.counts.capabilities} ${CAPABILITY.plural.toLowerCase()}`
    : `${row.capability.counts.openIssues} open`
  const counts = isProduct ? row.product.counts : row.capability.counts
  // One attention count per row: what needs a human first, else what is stuck.
  const attention = counts.needsYou || counts.blocked

  return (
    <div
      data-testid={`row-${row.key}`}
      role="treeitem"
      aria-level={row.depth + 1}
      aria-expanded={row.hasChildren ? row.expanded : undefined}
      className={indentClass(row.depth)}
    >
      <CardFrame
        id={row.key}
        openLabel={`Open ${noun.toLowerCase()} ${title}`}
        railClass={TIER_ACCENT[tier].rail}
        selected={selected}
        listMode={false}
        focusable
        swipeEnabled={false}
        onOpen={() => onOpen(row)}
        leading={
          row.hasChildren ? (
            <button
              type="button"
              aria-label={`${row.expanded ? 'Collapse' : 'Expand'} ${title}`}
              onClick={(e) => { e.stopPropagation(); onToggle(row.key) }}
              className={cn(
                'flex size-6 shrink-0 items-center justify-center rounded-sm text-ink-muted',
                'hover:bg-surface-raised focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
              )}
            >
              {row.expanded
                ? <ChevronDown size={14} aria-hidden="true" />
                : <ChevronRight size={14} aria-hidden="true" />}
            </button>
          ) : (
            <span aria-hidden="true" className="size-6 shrink-0" />
          )
        }
        identity={
          <span className="flex min-w-0 items-center gap-2">
            <TierDot tier={tier} />
            {isProduct
              ? <Package size={14} aria-hidden="true" className="shrink-0 text-ink-muted" />
              : <Layers size={14} aria-hidden="true" className="shrink-0 text-ink-muted" />}
            <span className={cn('truncate text-sm text-ink', isProduct ? 'font-semibold' : 'font-medium')}>
              {title}
            </span>
          </span>
        }
        trailing={
          attention > 0 ? (
            <>
              {/* The pill is drawn, the name is announced — one node carries the
                  whole string (`badgedName`), as everywhere else in the shell. */}
              <span className="sr-only">{badgedName({ label: tierLabel(tier), count: attention })}</span>
              <Badge tone={TIER_TONE[tier]} count={attention} aria-hidden="true" />
            </>
          ) : undefined
        }
      >
        <span className="block truncate text-xs text-ink-muted">{meta} · {tierLabel(tier)}</span>
      </CardFrame>
    </div>
  )
}
