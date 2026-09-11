// One CI-health row. Reads its own 7-day check insights rather than taking
// them as a prop — `CiScopeBody` renders one of these per tracked repo and
// each repo's insights are an independent, individually-cached fetch
// (`useCheckInsights`), the same shape as the automation-detail page's own
// per-row reads.
import { useCheckInsights } from '@/api/hooks'
import { CardFrame } from '@/components/card/CardFrame'
import { ciRepoKey } from '../deliveryNavState'
import { fmtSecs } from '../ci/format'

export interface CiRepoRowProps {
  repo: string
  selected: boolean
  onOpen: (repo: string) => void
}

export function CiRepoRow({ repo, selected, onOpen }: CiRepoRowProps) {
  const { data, isLoading } = useCheckInsights(repo, '7d', { poll: false })
  const key = ciRepoKey(repo)
  const slowest = data?.slowest[0]
  const line2 = isLoading
    ? 'Loading…'
    : slowest
      ? `${data.flakiest.length} flaky · slowest ${slowest.check_name} ${fmtSecs(slowest.p90)}`
      : 'No check history in 7d'
  return (
    <div data-testid={`row-${key}`} role="listitem" aria-current={selected ? 'true' : undefined}>
      <CardFrame
        id={repo}
        openLabel={`Open CI health ${repo}`}
        // Every repo starts at the same neutral rail — CI health has no tier
        // computed today (unlike an automation's), just its own text.
        railClass="bg-neutral/40"
        selected={selected}
        listMode={false}
        swipeEnabled={false}
        onOpen={onOpen}
        identity={<span className="truncate text-sm font-medium text-ink">{repo}</span>}
      >
        <span className="block truncate text-xs text-ink-muted">{line2}</span>
      </CardFrame>
    </div>
  )
}
