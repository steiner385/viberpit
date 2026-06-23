import { useState } from 'react'
import { GitPullRequest, Rocket } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { PullRequestInfo } from '@/api/types'
import { PrPill } from './PrPill'

export function PrList({ prs }: { prs?: PullRequestInfo[] | null }) {
  const [open, setOpen] = useState(false)
  // Defensive: the API always sends an array, but a stale payload or a test
  // fixture predating the prs field can pass undefined/null — treat as empty.
  const list = prs ?? []
  if (list.length === 0) return null
  if (list.length === 1) return <PrPill pr={list[0]} />

  const deployed = list.filter((p) => p.deployed).length
  return (
    <span className="relative inline-block">
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
        aria-expanded={open}
        aria-label={`${list.length} PRs${deployed ? `, ${deployed} deployed` : ''}`}
        className={cn(
          'inline-flex items-center gap-1 text-xs font-medium px-1.5 py-0.5 rounded',
          'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200',
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
            'absolute z-10 mt-1 left-0 flex flex-col gap-1 p-2 rounded-md',
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
