// Ported from plotroom@5166b52 frontend/src/HealthHeader.tsx; edits: row 5 — the
// `.health-*` and `.spine-glyph.s-*` stylesheet is gone. The band is built from
// ui/ primitives on token utilities (Button ghost chips + Badge dots tinted by
// LANE_TONE) and stays dense enough to sit inside `PageHeader.meta`.
import { useMemo } from 'react';
import { cn } from '@/lib/utils';
import { Badge } from '@/ui/Badge';
import { Button } from '@/ui/Button';
import type { DashboardState, LaneStatus } from './types';
import { buildLaneHealth } from './spine/laneHealth';
import { rollup, attentionPhrase, LANE_GLYPH, LANE_TONE, LANE_WORD } from './spine/laneStatus';

/** Global at-a-glance CI health band shown above the tabs. Reads the SAME lane
 *  derivations the Delivery spine renders in detail (via buildLaneHealth), so
 *  the overview and the detail tab can never disagree. A click routes to the
 *  richest detail for that lane (App owns the routing); the status is passed so
 *  PR CI can land on the matching Pipeline filter. */
export function HealthHeader({ state, onJumpToLane }: {
  state: DashboardState;
  onJumpToLane: (laneId: string | null, status: LaneStatus | null) => void;
}) {
  const lanes = useMemo(() => buildLaneHealth(state), [state]);
  const roll = useMemo(() => rollup(lanes), [lanes]);
  const allGreen = roll.state === 'green';
  const firstAttentionStatus =
    lanes.find((l) => l.id === roll.firstAttentionId)?.status ?? null;

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1" role="group" aria-label="Overall CI health">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="gap-1.5 px-2 font-semibold"
        data-testid="health-rollup"
        /* The rollup routes to the richest detail for the first lane needing
           attention — the legacy app's Delivery tab, or the matching workspace
           section. The hint stays destination-neutral so it's accurate in both. */
        aria-label={allGreen
          ? 'All lanes healthy'
          : `${attentionPhrase(roll.count)} — jump to the first`}
        onClick={() => onJumpToLane(roll.firstAttentionId, firstAttentionStatus)}
      >
        <Badge dot tone={LANE_TONE[roll.state]} />
        {allGreen ? 'All systems green' : attentionPhrase(roll.count)}
      </Button>
      <ul className="flex flex-wrap gap-1" role="list">
        {lanes.map((l) => (
          <li key={l.id}>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className={cn('gap-1.5 px-2 font-normal',
                l.wiredness === 'not-wired' && 'opacity-60')}
              data-testid={`health-lane-${l.id}`}
              aria-label={`${l.title}: ${LANE_WORD[l.status]} — ${l.summary}`}
              title={`${l.title} — ${l.summary}`}
              onClick={() => onJumpToLane(l.id, l.status)}
            >
              <Badge dot tone={LANE_TONE[l.status]} />
              {/* The glyph is what tells blind (◌) from idle (·) — they share
                  the neutral tone, so shape carries the difference. */}
              <span aria-hidden="true">{LANE_GLYPH[l.status]}</span>
              <span>{l.title}</span>
              {/* #189: surface the already-computed lane summary inline on
                  non-green chips (was hover-only); the full text stays in aria-label. */}
              {l.status !== 'green' && l.summary && (
                <span className="max-w-48 truncate text-xs text-ink-muted" aria-hidden="true">{l.summary}</span>
              )}
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}
