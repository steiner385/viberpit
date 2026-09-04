// Ported from plotroom@5166b52 frontend/src/StatusStrip.tsx; edits: import repointing (./types); eslint-disable react-refresh/only-export-components (mixed export file — intentional port); latestFailed.current sync moved to useLayoutEffect (react-hooks/refs lint gate, same convention as useDashboard.ts)
/* eslint-disable react-refresh/only-export-components */
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useInteractive } from '@/shell/InteractionContext';
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

interface TileConfig { bucket: Bucket; label: string; cssClass: string; title: string; }

/** One-line bucket definitions — surfaced as the tile tooltip and reused by the
 *  legend panel so the two can never drift apart. */
export const TILE_DEFINITIONS: TileConfig[] = [
  { bucket: 'running', label: 'CI running',     cssClass: 'tile-running',
    title: 'CI running on the head commit' },
  { bucket: 'queued',  label: 'In queue',        cssClass: 'tile-queued',
    title: 'In the merge queue — building or waiting for a slot' },
  { bucket: 'deploy',  label: 'Deploying',       cssClass: 'tile-deploy',
    title: 'Merged — deploying to QA or awaiting production deploy' },
  { bucket: 'failed',  label: 'Failed',          cssClass: 'tile-failed',
    title: 'CI failed, merge-group build failed, or armed but stuck (not enqueued)' },
  { bucket: 'ready',   label: 'Ready',           cssClass: 'tile-ready',
    title: 'Open, checks green — actionable (ready to arm auto-merge or merge directly)' },
  { bucket: 'merged',  label: 'Merged / other',  cssClass: 'tile-merged',
    title: 'Recently merged (7-day retention), draft, or conflicting — no action needed' },
];

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
      <span className={`tile-delta tile-delta--${failedDelta > 0 ? 'up' : 'down'}`} aria-hidden="true">
        {failedDelta > 0 ? `▲${failedDelta}` : `▼${-failedDelta}`}
      </span>
    ) : null;

  return (
    <div className="status-strip" role="group" aria-label="Status overview">
      {TILES.map(({ bucket, label, cssClass, title }) => {
        const count = counts.get(bucket) ?? 0;
        if (!interactive) {
          // read-only tile: same look, no filter affordance (empty buckets
          // dim via .zero, mirroring the disabled button state)
          return (
            <div key={bucket}
              className={`status-tile ${cssClass}${count === 0 ? ' zero' : ''}`}
              title={title}>
              <b>{count}</b>
              <span>{label}</span>
              {deltaBadge(bucket)}
            </div>
          );
        }
        const isActive = activeFilter === bucket;
        const disabled = count === 0 && !isActive;
        return (
          <button
            key={bucket}
            type="button"
            className={`status-tile ${cssClass}${isActive ? ' active' : ''}`}
            /* tooltip doubles as the accessible description; the visible
               count + label stays the accessible name */
            title={title}
            aria-pressed={isActive}
            disabled={disabled}
            onClick={() => onFilter(isActive ? null : bucket)}
          >
            <b>{count}</b>
            <span>{label}</span>
            {deltaBadge(bucket)}
          </button>
        );
      })}
    </div>
  );
}
