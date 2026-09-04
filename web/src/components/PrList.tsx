import { useState } from 'react'
import { GitPullRequest, Rocket } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { PullRequestInfo } from '@/api/types'
import { anyChecksFailing } from '@/session/prStatus'
import { PrPill } from './PrPill'

const CHIP_NEUTRAL = 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200'
const CHIP_FAILING = 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300'

/**
 * Tints the collapsed "N PRs" chip from its hidden contents. Binary by design:
 * a quiet red warning when any contained PR is failing-and-actionable, else
 * neutral. Routed through `anyChecksFailing` — the SAME predicate that lifts
 * the card into "Failed" — so the chip color and the triage group can
 * never disagree (and a terminal closed/merged PR's frozen red is ignored here
 * too, for free).
 */
function chipTone(prs: PullRequestInfo[]): string {
  return anyChecksFailing(prs) ? CHIP_FAILING : CHIP_NEUTRAL
}

export function PrList({ prs }: { prs?: PullRequestInfo[] | null }) {
  const [open, setOpen] = useState(false)
  // Defensive: the API always sends an array, but a stale payload or a test
  // fixture predating the prs field can pass undefined/null — treat as empty.
  const list = prs ?? []
  if (list.length === 0) return null
  if (list.length === 1) return <PrPill pr={list[0]} />

  const deployed = list.filter((p) => p.deployed).length
  const failing = anyChecksFailing(list)
  return (
    <span className="relative inline-block">
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
        aria-expanded={open}
        aria-label={`${list.length} PRs${deployed ? `, ${deployed} deployed` : ''}${failing ? ', checks failing' : ''}`}
        className={cn(
          'inline-flex items-center gap-1 text-xs font-medium px-1.5 py-0.5 rounded',
          chipTone(list),
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
        )}
      >
        <GitPullRequest size={11} aria-hidden />
        <span className="tabular-nums">{list.length} PRs</span>
        {deployed > 0 && (
          <span className="inline-flex items-center gap-0.5 text-emerald-600 dark:text-emerald-400">
            <Rocket size={11} aria-hidden /><span className="tabular-nums">{deployed}</span>
          </span>
        )}
      </button>
      {open && (
        <div
          className={cn(
            // z-20 keeps the open popover above the sticky group headers
            // (GroupHeader: `sticky top-0 z-10`); at an equal z-index a later
            // group's header would paint over a popover opened from a card above it.
            'absolute z-20 mt-1 left-0 flex flex-col gap-1 p-2 rounded-md',
            'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 shadow-lg',
          )}
          onClick={(e) => e.stopPropagation()}
        >
          {list.map((p) => <PrPill key={p.number} pr={p} />)}
        </div>
      )}
    </span>
  )
}
