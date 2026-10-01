// One CI-health row. Reads its own 7-day check insights rather than taking
// them as a prop — `CiScopeBody` renders one of these per tracked repo and
// each repo's insights are an independent, individually-cached fetch
// (`useCheckInsights`), the same shape as the automation-detail page's own
// per-row reads.
import { useCheckInsights } from '@/api/hooks'
import { CardFrame } from '@/ui/list/CardFrame'
import { TONE_FILL } from '@/ui/Badge'
import { LANE_TONE, LANE_WORD } from '@/pipeline/spine/laneStatus'
import { ciRepoKey } from '../deliveryNavState'
import { ciHealth, type CiThresholds } from '../ci/ciHealth'
import { fmtSecs } from '../ci/format'

export interface CiRepoRowProps {
  repo: string
  selected: boolean
  /** The CI/CD Doctor's flake thresholds (`ciThresholds`), or null until its
   *  row has loaded — then flake rates are counted but not judged. */
  thresholds: CiThresholds | null
  /** The list's roving tab stop (`ui/list/useRovingRows`); see `AutomationRow`. */
  focusable?: boolean
  idScope?: string
  onOpen: (repo: string) => void
}

export function CiRepoRow({ repo, selected, thresholds, focusable, idScope, onOpen }: CiRepoRowProps) {
  const { data, isLoading } = useCheckInsights(repo, '7d', { poll: false })
  const key = ciRepoKey(repo)
  const slowest = data?.slowest[0]
  // One verdict per repo in the pipeline's lane vocabulary — the rail is the
  // tone, the word leads line 2 (2026-10-01 Delivery critique, P2: a neutral
  // rail and "1 flaky" left the reader to know the thresholds).
  const health = ciHealth(data, thresholds)
  // "No history" is the verdict's call, not the timing table's: a repo with no
  // run timings in the window can still carry live regressions (the probe
  // found KinDash on an amber rail over "No check history").
  const line2 = isLoading
    ? 'Loading…'
    : health.status === 'idle' || !data
      ? 'No check history in 7d'
      : [
          LANE_WORD[health.status],
          ...health.reasons,
          ...(thresholds || data.flakiest.length === 0 ? [] : [`${data.flakiest.length} flaky`]),
          ...(slowest ? [`slowest ${slowest.check_name} ${fmtSecs(slowest.p90)}`] : []),
        ].join(' · ')
  // A calm lane keeps the muted rail the idle tier reads in; a verdict takes
  // its full tone.
  const railClass = health.status === 'idle' || isLoading ? 'bg-neutral/20' : TONE_FILL[LANE_TONE[health.status]]
  return (
    <div data-testid={`row-${key}`} data-row-key={repo} role="listitem" aria-current={selected ? 'true' : undefined}>
      <CardFrame
        id={repo}
        idScope={idScope}
        openLabel={`Open CI health ${repo}`}
        railClass={railClass}
        selected={selected}
        focusable={focusable}
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
