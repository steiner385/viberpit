// The product Overview's one line about milestones (spec 2026-11-05 §9):
// "Milestones: <current> · n of m gate conditions met", linking to the page.
// Renders nothing when the product has no ladder.
import { Link } from 'react-router-dom'
import { useMilestones } from '@/api/milestones'
import { milestonesPath } from '@/features/roadmap/roadmapNavState'
import { currentMilestone, gateLine, loadedView, milestoneTitle } from './format'

export function MilestonesSummaryBlock({ productSlug }: { productSlug: string }) {
  const { data } = useMilestones(productSlug)
  const view = loadedView(data)
  if (!view) return null
  const current = currentMilestone(view)
  return (
    <p className="text-xs text-ink-muted">
      <Link to={milestonesPath(productSlug)} className="font-medium text-accent hover:underline">Milestones</Link>
      {': '}
      {current
        ? `${milestoneTitle(current)} · ${gateLine(current.gateSummary)}`
        : 'no current milestone'}
    </p>
  )
}
