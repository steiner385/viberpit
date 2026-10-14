// One row of the Map scope's tree: a product, a capability, or one of the two
// pseudo rows a product hangs off itself (awaiting triage / needs a home).
// Initiatives get their own card — they carry a stage chip and a reason.
//
// The frame is `CardFrame`, the same one the sessions list uses, so a roadmap
// row and a session row share their rail, selection ring and open control.
// Tokens only — this file is under `src/features/`.
import { HelpCircle, Inbox, Layers, Package } from 'lucide-react'
import { cn } from '@/lib/utils'
import { CardFrame } from '@/ui/list/CardFrame'
import { Badge } from '@/ui/Badge'
import { badgedName } from '@/ui/badgedName'
import { CAPABILITY, PRODUCT } from '@/features/products/vocab'
import { TIER_ACCENT, TIER_TONE, tierLabel } from '../roadmapTier'
import type { TreeRow } from '../treeRows'
import { needsYouCount, waitingSummary } from '../detail/needsYou'
import { indentClass } from './indent'
import { TierDot } from './TierDot'
import { FoldChevron } from './FoldChevron'

export interface TreeRowCardProps {
  row: Exclude<TreeRow, { kind: 'initiative' }>
  selected: boolean
  /** Namespace for the open control's DOM id (`ui/list/rowId`) — the Roadmap
   *  tree's scope, so its `getElementById` focus can never land on an
   *  identically-keyed row in the detail pane beside it. */
  idScope?: string
  /** Roving tabindex: exactly one row of the tree is a tab stop (`useTreeKeys`).
   *  Everything else is reached with the arrow keys, as a tree should be. */
  focusable: boolean
  onOpen: (row: TreeRow) => void
  onToggle: (key: string) => void
  onOpenInbox: (productSlug: string) => void
}

export function TreeRowCard({ row, selected, idScope, focusable, onOpen, onToggle, onOpenInbox }: TreeRowCardProps) {
  if (row.kind === 'pseudo') {
    const label = row.pseudo === 'awaiting-triage' ? 'Awaiting triage' : 'Needs a home'
    const Icon = row.pseudo === 'awaiting-triage' ? Inbox : HelpCircle
    return (
      <div
        data-testid={`row-${row.key}`}
        data-row-key={row.key}
        role="treeitem"
        aria-level={row.depth + 1}
        aria-selected={selected}
        className={indentClass(row.depth)}
      >
        <CardFrame
          id={row.key}
          idScope={idScope}
          openLabel={`Open ${label.toLowerCase()} for ${row.product.name ?? row.product.slug}`}
          railClass={TIER_ACCENT['needs-you'].rail}
          selected={selected}
          listMode={false}
          focusable={focusable}
          swipeEnabled={false}
          onOpen={() => onOpenInbox(row.product.slug)}
          leading={<span aria-hidden="true" className="size-6 shrink-0" />}
          identity={
            <span className="flex min-w-0 items-center gap-2 text-sm text-ink">
              <Icon size={14} aria-hidden="true" className="shrink-0 text-ink-muted" />
              <span className="truncate">{label}</span>
            </span>
          }
          // Outside the open button, or the count would be swallowed by that
          // button's `aria-label` and never announced at all.
          trailing={
            <>
              <span className="sr-only">{badgedName({ label, count: row.count })}</span>
              <Badge tone="warning" count={row.count} aria-hidden="true" />
            </>
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
  const attention = needsYouCount([row.product], isProduct ? undefined : row.capability)
  // Waiting is said, never badged (design 2026-10-14 §1): stopped on other work,
  // not on you. The product row only — the counters are per product.
  const waiting = isProduct ? waitingSummary(row.product) : []
  const waitingText = waiting.map(({ label, count }) => `${count} ${label}`)

  return (
    <div
      data-testid={`row-${row.key}`}
      data-row-key={row.key}
      role="treeitem"
      aria-level={row.depth + 1}
      aria-selected={selected}
      aria-expanded={row.hasChildren ? row.expanded : undefined}
      className={indentClass(row.depth)}
    >
      <CardFrame
        id={row.key}
        idScope={idScope}
        openLabel={`Open ${noun.toLowerCase()} ${title}`}
        railClass={TIER_ACCENT[tier].rail}
        selected={selected}
        listMode={false}
        focusable={focusable}
        swipeEnabled={false}
        onOpen={() => onOpen(row)}
        leading={
          row.hasChildren ? (
            <FoldChevron expanded={row.expanded} label={title} onToggle={() => onToggle(row.key)} />
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
          attention > 0 || (!isProduct && row.capability.prNumber != null) ? (
            <>
              {attention > 0 && (
                <>
                  {/* The pill is drawn, the name is announced — one node carries the
                      whole string (`badgedName`), as everywhere else in the shell. */}
                  <span className="sr-only">{badgedName({ label: tierLabel(tier), count: attention })}</span>
                  <Badge tone={TIER_TONE[tier]} count={attention} aria-hidden="true" />
                </>
              )}
              {/* Changes to this capability are proposed but not yet merged —
                  R8c-3's one-line note, mirrored here so the tree itself says so.
                  Deliberately NOT `openMapChanges(product)`: the badge is the
                  capability's OWN fact — the sync clears `prNumber` when the PR
                  merges — and the row does not always have its product's pointer
                  to read anyway. */}
              {!isProduct && row.capability.prNumber != null && (
                <>
                  <span className="sr-only">{`proposed in pull request ${row.capability.prNumber}`}</span>
                  <Badge tone="info" aria-hidden="true">PR #{row.capability.prNumber}</Badge>
                </>
              )}
            </>
          ) : undefined
        }
      >
        <span className="block truncate text-xs text-ink-muted">{meta} · {tierLabel(tier)}</span>
        {waitingText.length > 0 && (
          <span
            data-testid="waiting-summary"
            className="block truncate text-xs text-ink-muted"
            title={`Waiting:\n${waitingText.join('\n')}`}
          >
            Waiting: {waitingText.join(' · ')}
          </span>
        )}
      </CardFrame>
    </div>
  )
}
