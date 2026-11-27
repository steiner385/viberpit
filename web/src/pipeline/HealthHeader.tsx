// Ported from plotroom@5166b52 frontend/src/HealthHeader.tsx; edits: row 5 — the
// `.health-*` and `.spine-glyph.s-*` stylesheet is gone. The band is built from
// ui/ primitives on token utilities (Button ghost chips + Badge dots tinted by
// LANE_TONE) and stays dense enough to sit inside `PageHeader.meta`.
import { useMemo } from 'react';
import { cn } from '@/lib/utils';
import { Badge, TONE_TEXT } from '@/ui/Badge';
import { Button } from '@/ui/Button';
import { TOUCH_FLOOR } from '@/ui/list/listControlStyles';
import { useInteractive } from '@/shell/InteractionContext';
import type { DashboardState, LaneStatus } from './types';
import { buildLaneHealth } from './spine/laneHealth';
import { rollup, attentionLanes, attentionPhrase, quietLaneSummary, LANE_GLYPH, LANE_TONE, LANE_WORD } from './spine/laneStatus';

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
  // Below md only the lanes that need attention get a chip — with their word,
  // not a bare glyph — and the rest fold into one muted count. Seven glyph-only
  // dots (✗ ● ◐ · ◌) at 34–42×36 read as nothing on a phone (2026-09-30
  // Pipeline critique, P2 / heuristic 6). `attentionLanes` is the rollup's own
  // definition, so the chips shown are exactly the ones the rollup counted.
  const attention = useMemo(() => new Set(attentionLanes(lanes).map((l) => l.id)), [lanes]);
  const quiet = lanes.filter((l) => !attention.has(l.id));
  const firstAttentionStatus =
    lanes.find((l) => l.id === roll.firstAttentionId)?.status ?? null;
  // Kiosk (shell spec §5): a wall display has no one to click these — unlike
  // `PageHeader`'s `actions` cluster (omitted outright) these chips carry the
  // health signal itself, so they stay visible and render `aria-disabled`
  // rather than disappearing. `Button` does not read InteractionContext
  // itself (every caller decides what "kiosk" means for its own surface —
  // `StatusStrip`'s tiles become plain divs instead), so the header gates the
  // click here (#1555).
  const interactive = useInteractive();

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1" role="group" aria-label="Overall CI health">
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className={cn('gap-1.5 px-2 font-semibold', TOUCH_FLOOR)}
        data-testid="health-rollup"
        /* The rollup routes to the richest detail for the first lane needing
           attention — the legacy app's Delivery tab, or the matching workspace
           section. The hint stays destination-neutral so it's accurate in both. */
        aria-label={allGreen
          ? 'All lanes healthy'
          : `${attentionPhrase(roll.count)} — jump to the first`}
        aria-disabled={!interactive || undefined}
        onClick={() => interactive && onJumpToLane(roll.firstAttentionId, firstAttentionStatus)}
      >
        <Badge dot tone={LANE_TONE[roll.state]} />
        {allGreen ? 'All systems green' : attentionPhrase(roll.count)}
      </Button>
      <ul className="flex flex-wrap gap-1" role="list">
        {lanes.map((l) => (
          <li key={l.id} className={attention.has(l.id) ? undefined : 'hidden md:block'}>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              /* A lane with no data source recedes through the muted ink TOKEN, not
                 `opacity-60`: opacity put the title at 4.3:1 and the summary (already
                 muted) at 2.4:1 against the 4.5:1 floor (#1416, SC 1.4.3). */
              className={cn('gap-1.5 px-2 font-normal', TOUCH_FLOOR,
                l.wiredness === 'not-wired' && 'text-ink-muted')}
              data-testid={`health-lane-${l.id}`}
              title={`${l.title} — ${l.summary}`}
              aria-disabled={!interactive || undefined}
              onClick={() => interactive && onJumpToLane(l.id, l.status)}
            >
              {/* One mark per lane: the glyph in its tone. The chip used to lead
                  with a tone dot AND the glyph ("● ● Merge queue", "● · Scheduled"),
                  two marks saying one thing. Shape still tells blind (◌) from
                  idle (·), which share the neutral tone. */}
              <span aria-hidden="true" data-testid={`health-glyph-${l.id}`}
                className={cn('inline-block w-3 text-center font-semibold', TONE_TEXT[LANE_TONE[l.status]])}>
                {LANE_GLYPH[l.status]}
              </span>
              {/* Below md a QUIET lane is folded away entirely (above), so the
                  only chips left are the attention ones and they can afford
                  their word: the ≤2-row budget (#1555) holds with the count
                  line in place of seven titled chips. */}
              <span className={attention.has(l.id) ? 'inline' : 'hidden md:inline'}>{l.title}</span>
              {/* The chip is named by its CONTENT — the visible title, then these words —
                  not by an `aria-label`, which replaced the content and so dropped the
                  title a voice-control user reads off the screen (#1416, SC 2.5.3). The
                  status word and summary live here because the visible summary below is
                  truncated, and absent below `md`. */}
              <span className="sr-only">{`: ${LANE_WORD[l.status]} — ${l.summary}`}</span>
              {/* #189: surface the already-computed lane summary inline on
                  non-green chips (was hover-only); the full text is the sr-only span above. */}
              {l.status !== 'green' && l.summary && (
                <span className="hidden max-w-48 truncate text-xs text-ink-muted md:inline" aria-hidden="true">{l.summary}</span>
              )}
            </Button>
          </li>
        ))}
        {quiet.length > 0 && (
          <li className="self-center text-xs text-ink-muted md:hidden" data-testid="health-quiet-lanes">
            {quietLaneSummary(quiet)}
          </li>
        )}
      </ul>
    </div>
  );
}
