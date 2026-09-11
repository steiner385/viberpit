// Ported from plotroom@5166b52 frontend/src/StatusStrip.tsx; edits: import repointing (./types); eslint-disable react-refresh/only-export-components (mixed export file — intentional port); latestFailed.current sync moved to useLayoutEffect (react-hooks/refs lint gate, same convention as useDashboard.ts); row 5 — the `.status-tile`/`.tile-*` stylesheet is gone: tiles are token utilities washed by TILE_TONE through the Badge tone maps
/* eslint-disable react-refresh/only-export-components */
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useInteractive } from '@/shell/InteractionContext';
import { cn } from '@/lib/utils';
import { TONE_TEXT, TONE_WASH, type Tone } from '@/ui/Badge';
import type { PrView } from './types';

export type Bucket = 'running' | 'queued' | 'deploy' | 'failed' | 'ready' | 'merged';

/** Map a PrView to its status bucket. */
export function bucketPr(pr: PrView): Bucket {
  const { stage, substate } = pr.stage;
  if (stage === 'ci') return 'running';
  if (stage === 'queue') {
    if (substate === 'group-failed') return 'failed';
    return 'queued';
  }
  // qa-deploy / awaiting-prod only exist for repos with deploy environments;
  // bare 'merged' is the retention-window stage for repos WITHOUT deploys and
  // must not inflate the deploy ("Deploying") tile.
  if (stage === 'qa-deploy' || stage === 'awaiting-prod') return 'deploy';
  if (stage === 'parked') {
    if (substate === 'ci-failed') return 'failed';
    return 'merged'; // draft / conflicting / other parked — retention noise
  }
  if (stage === 'ready') {
    // Armed + BLOCKED + not in queue: GitHub silently refused to enqueue.
    // Needs attention — surface in the Failed tile, not the Ready tile.
    if (substate === 'stuck-enqueue') return 'failed';
    return 'ready';
  }
  // merged stage (7-day retention window) + any other unknown stage
  return 'merged';
}

/** Is the PR failed? The single source of truth — derived from bucketPr so the
 *  status-strip tile, the repo summary, and the pipeline header can never
 *  disagree about what "failed" means. */
export function isFailedPr(pr: PrView): boolean {
  return bucketPr(pr) === 'failed';
}

/** Is the PR moving through an active pipeline stage (CI → queue → QA deploy)?
 *  Excludes awaiting-prod (waiting, not working) and parked/ready/merged. Shared
 *  so the legacy summary and the workspace pipeline header classify identically. */
export function isActivePr(pr: PrView): boolean {
  const { stage } = pr.stage;
  return stage === 'ci' || stage === 'queue' || stage === 'qa-deploy';
}

/** Bucket → design-system tone: the one place a status bucket picks a colour.
 *  Both the tiles below and the legend's bucket list read it, so the board and
 *  the legend cannot disagree about what "Failed" looks like. */
export const TILE_TONE: Record<Bucket, Tone> = {
  running: 'info', queued: 'accent', deploy: 'success',
  failed: 'danger', ready: 'warning', merged: 'neutral',
};

interface TileConfig { bucket: Bucket; label: string; tone: Tone; title: string; }

/** One-line bucket definitions — surfaced as the tile tooltip and reused by the
 *  legend panel so the two can never drift apart. */
export const TILE_DEFINITIONS: TileConfig[] = [
  { bucket: 'running', label: 'CI running',     tone: TILE_TONE.running,
    title: 'CI running on the head commit' },
  { bucket: 'queued',  label: 'In queue',        tone: TILE_TONE.queued,
    title: 'In the merge queue — building or waiting for a slot' },
  { bucket: 'deploy',  label: 'Deploying',       tone: TILE_TONE.deploy,
    title: 'Merged — deploying to QA or awaiting production deploy' },
  { bucket: 'failed',  label: 'Failed',          tone: TILE_TONE.failed,
    title: 'CI failed, merge-group build failed, or armed but stuck (not enqueued)' },
  { bucket: 'ready',   label: 'Ready',           tone: TILE_TONE.ready,
    title: 'Open, checks green — actionable (ready to arm auto-merge or merge directly)' },
  { bucket: 'merged',  label: 'Merged / other',  tone: TILE_TONE.merged,
    title: 'Recently merged (7-day retention), draft, or conflicting — no action needed' },
];

/** The tile box: one wash-on-edge card per bucket, interactive or not. */
const TILE_BASE = 'flex min-h-target flex-1 basis-24 flex-col gap-0.5 rounded-md border border-edge px-3 py-2 text-left';

const TILES = TILE_DEFINITIONS;

interface StatusStripProps {
  prs: PrView[];
  activeFilter: Bucket | null;
  onFilter: (bucket: Bucket | null) => void;
}

export function StatusStrip({ prs, activeFilter, onFilter }: StatusStripProps) {
  // false in kiosk mode (issue #20), via InteractionContext: tiles render as
  // plain glanceable divs instead of filter buttons.
  const interactive = useInteractive();
  const counts = new Map<Bucket, number>();
  for (const p of prs) {
    const b = bucketPr(p);
    counts.set(b, (counts.get(b) ?? 0) + 1);
  }

  // #189: a rising failed count is the primary "something just broke" signal.
  // Show its delta vs a baseline that resets every 5 minutes, so a spike within
  // the window is visible (up = rising, down = falling). Other tiles get no
  // delta — a changing queue/deploy/idle count isn't an urgency signal.
  const failedCount = counts.get('failed') ?? 0;
  const latestFailed = useRef(failedCount);
  useLayoutEffect(() => { latestFailed.current = failedCount; }, [failedCount]);
  const [failedBaseline, setFailedBaseline] = useState(failedCount);
  useEffect(() => {
    const id = setInterval(() => setFailedBaseline(latestFailed.current), 5 * 60_000);
    return () => clearInterval(id);
  }, []);
  const failedDelta = failedCount - failedBaseline;
  const deltaBadge = (bucket: Bucket) =>
    bucket === 'failed' && failedDelta !== 0 ? (
      <span
        className={cn('text-2xs font-bold leading-none',
          failedDelta > 0 ? TONE_TEXT.danger : TONE_TEXT.success)}
        aria-hidden="true"
      >
        {failedDelta > 0 ? `▲${failedDelta}` : `▼${-failedDelta}`}
      </span>
    ) : null;

  const body = (count: number, label: string, tone: Tone, bucket: Bucket) => (
    <>
      <b className={cn('text-xl font-semibold leading-tight tabular-nums', TONE_TEXT[tone])}>{count}</b>
      <span className="text-2xs uppercase tracking-wide text-ink-muted">{label}</span>
      {deltaBadge(bucket)}
    </>
  );

  return (
    <div className="mb-3 flex flex-wrap gap-2" role="group" aria-label="Status overview">
      {TILES.map(({ bucket, label, tone, title }) => {
        const count = counts.get(bucket) ?? 0;
        if (!interactive) {
          // read-only tile: same look, no filter affordance (empty buckets dim,
          // mirroring the disabled button state)
          return (
            <div key={bucket}
              className={cn(TILE_BASE, TONE_WASH[tone], count === 0 && 'opacity-50')}
              title={title}>
              {body(count, label, tone, bucket)}
            </div>
          );
        }
        const isActive = activeFilter === bucket;
        const disabled = count === 0 && !isActive;
        return (
          <button
            key={bucket}
            type="button"
            className={cn(TILE_BASE, TONE_WASH[tone],
              'transition-opacity hover:opacity-90 disabled:opacity-50',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
              isActive && 'ring-2 ring-accent')}
            /* tooltip doubles as the accessible description; the visible
               count + label stays the accessible name */
            title={title}
            aria-pressed={isActive}
            disabled={disabled}
            onClick={() => onFilter(isActive ? null : bucket)}
          >
            {body(count, label, tone, bucket)}
          </button>
        );
      })}
    </div>
  );
}
