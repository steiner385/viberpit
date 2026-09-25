// Ported from plotroom@5166b52 frontend/src/Waterfall.tsx; edits: import repointing (./types, ./leadtime, ./format); eslint-disable react-refresh/only-export-components (mixed export file — intentional port)
/* eslint-disable react-refresh/only-export-components */
import { useId } from 'react';
import type { LeadTimeSegmentId, PrTimeline } from '@/pipeline/types';
import { LEAD_TIME_SEGMENTS } from '@/pipeline/leadtime';
import { formatDur } from '@/pipeline/format';

/* #172: a distinct white-line texture per segment, overlaid on the segment color,
   so the waterfall is readable under colour-vision deficiency + grayscale (not by
   the 5 fills alone). null = solid baseline. Path is drawn in an 8×8 tile. */
const SEG_PATTERN: Record<LeadTimeSegmentId, string | null> = {
  toFirstGreen: null,                  // solid (baseline)
  greenToEnqueued: 'M0,4 H8',          // horizontal
  queue: 'M0,8 L8,0',                  // diagonal
  qaDeploy: 'M0,8 L8,0 M0,0 L8,8',     // crosshatch
  awaitingProd: 'M4,0 V8',             // vertical
};

/**
 * Per-PR "where did the time go" waterfall (issue #50): one horizontal bar per
 * pipeline segment of a merged PR's timeline, on one shared time scale —
 * the artifact you paste into an incident retro. Colors reuse the Metrics
 * lead-time panel legend (shared LEAD_TIME_SEGMENTS metadata), so the per-PR
 * view and the fleet view always read the same.
 *
 * Segments are built strictly pairwise: a segment renders ONLY when both of
 * its endpoint timestamps exist and parse (end ≥ start). Missing waypoints
 * leave honest gaps — nothing is fabricated. CI attempt detail is out of
 * scope for v1 (the spine timestamps only).
 */

export interface WaterfallSegment {
  id: LeadTimeSegmentId;
  label: string;
  color: string;
  startMs: number;
  endMs: number;
}

/** The (from, to) timeline waypoints behind each lead-time segment id. */
const SEGMENT_ENDPOINTS: { id: LeadTimeSegmentId; from: keyof PrTimeline; to: keyof PrTimeline }[] = [
  { id: 'toFirstGreen', from: 'createdAt', to: 'firstGreenAt' },
  { id: 'greenToEnqueued', from: 'firstGreenAt', to: 'enqueuedAt' },
  { id: 'queue', from: 'enqueuedAt', to: 'mergedAt' },
  { id: 'qaDeploy', from: 'mergedAt', to: 'qaLiveAt' },
  { id: 'awaitingProd', from: 'qaLiveAt', to: 'prodLiveAt' },
];

const META = new Map(LEAD_TIME_SEGMENTS.map((s) => [s.id, s]));

/** Pairwise segment extraction: both endpoints present + parseable + end ≥ start. */
export function waterfallSegments(t: PrTimeline): WaterfallSegment[] {
  const out: WaterfallSegment[] = [];
  for (const { id, from, to } of SEGMENT_ENDPOINTS) {
    const a = t[from]; const b = t[to];
    if (!a || !b) continue;
    const startMs = Date.parse(a); const endMs = Date.parse(b);
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs < startMs) continue;
    const m = META.get(id)!;
    out.push({ id, label: m.label, color: m.color, startMs, endMs });
  }
  return out;
}

// Geometry in viewBox units; the svg scales to the panel width (width:100%).
// #2418: the three <text> elements below set a real CSS `font-size: var(--fs-base)`
// (via `style`, not the `fontSize` JSX prop — that maps to the plain SVG attribute
// and would bypass the kiosk --scale cascade) instead of a bare unitless number, so
// kiosk's `--scale: 1.25` (AppShell.tsx, same mechanism plotroom.css uses for the
// other three charts — #1407) enlarges this chart's own labels too. A CSS length
// inside a viewBox-scaled <svg> renders through the SAME viewBox→rendered-width
// transform as a unitless attribute value would, so FONT below is kept as the plain
// number matching var(--fs-base) at --scale:1 (1rem = 16px, index.css) — every
// geometry constant that depends on text metrics is re-derived off it (was tuned
// around the old FONT=11 literal). PAD_L/PAD_R are widened to keep the longest
// label ('green → enqueued') and duration text from clipping once kiosk's 1.25×
// makes the effective rendered size exceed this base number.
const VB_W = 1000;
const PAD_L = 200;   // segment-label column
const PAD_R = 80;    // duration-label column
const ROW_H = 32;
const BAR_H = 16;
const PAD_T = 6;
const AXIS_H = 26;
const FONT = 16;

/** Local wall-clock for tooltips ('Jun 10, 09:30'). */
function localTime(ms: number): string {
  return new Date(ms).toLocaleString(undefined,
    { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
}

export function Waterfall({ timeline }: { timeline: PrTimeline }) {
  const uid = useId(); // unique pattern ids per instance (avoids url(#) collisions)
  const segs = waterfallSegments(timeline);
  if (!segs.length) return null;
  const t0 = Math.min(...segs.map((s) => s.startMs));
  const t1 = Math.max(...segs.map((s) => s.endMs));
  const spanMs = (t1 - t0) || 1;
  const x = (ms: number): number => PAD_L + ((ms - t0) / spanMs) * (VB_W - PAD_L - PAD_R);
  const height = PAD_T + segs.length * ROW_H + AXIS_H;
  const axisY = PAD_T + segs.length * ROW_H;
  // hour/minute axis labels at the origin, midpoint, and full span
  const ticks = [0, spanMs / 2, spanMs].map((off, i) => ({
    off, text: i === 0 ? '0m' : formatDur(off / 1000),
  }));
  const svgAriaLabel = 'Lead-time waterfall: '
    + segs.map((s) => `${s.label} ${formatDur((s.endMs - s.startMs) / 1000)}`).join(', ');
  return (
    <div className="pipeline-chart waterfall" data-testid="waterfall">
      <svg className="pipeline-chart chart-svg" width="100%" viewBox={`0 0 ${VB_W} ${height}`}
        role="img" aria-label={svgAriaLabel}>
        <defs>
          {segs.map((s) => SEG_PATTERN[s.id] && (
            <pattern key={s.id} id={`${uid}-${s.id}`} patternUnits="userSpaceOnUse" width="8" height="8">
              <path d={SEG_PATTERN[s.id]!} stroke="var(--t-canvas)" strokeOpacity="0.32" strokeWidth="1.4" />
            </pattern>
          ))}
        </defs>
        {ticks.map((tk, i) => (
          <g key={`tick${i}`}>
            <line x1={x(t0 + tk.off)} x2={x(t0 + tk.off)} y1={PAD_T} y2={axisY}
              stroke="var(--t-edge)" strokeDasharray="3 4" />
            <text x={x(t0 + tk.off)} y={axisY + FONT + 2} style={{ fontSize: 'var(--fs-base)' }} fill="var(--t-ink-muted)"
              textAnchor={i === 0 ? 'start' : i === ticks.length - 1 ? 'end' : 'middle'}>
              {tk.text}
            </text>
          </g>
        ))}
        {segs.map((s, i) => {
          const y = PAD_T + i * ROW_H;
          const durSecs = (s.endMs - s.startMs) / 1000;
          const w = Math.max(x(s.endMs) - x(s.startMs), 2);
          return (
            <g key={s.id} data-testid={`waterfall-seg-${s.id}`}>
              <title>{`${s.label}: ${formatDur(durSecs)} (${localTime(s.startMs)} → ${localTime(s.endMs)}) — ${META.get(s.id)!.desc}`}</title>
              <text x={PAD_L - 8} y={y + ROW_H / 2 + FONT / 2 - 1} textAnchor="end"
                style={{ fontSize: 'var(--fs-base)' }} fill="var(--t-ink-muted)">{s.label}</text>
              <rect x={x(s.startMs)} y={y + (ROW_H - BAR_H) / 2} width={w} height={BAR_H}
                rx={2} fill={s.color} />
              {SEG_PATTERN[s.id] && (
                <rect x={x(s.startMs)} y={y + (ROW_H - BAR_H) / 2} width={w} height={BAR_H}
                  rx={2} fill={`url(#${uid}-${s.id})`} />
              )}
              <text data-testid={`waterfall-dur-${s.id}`}
                x={VB_W - 4} y={y + ROW_H / 2 + FONT / 2 - 1} textAnchor="end"
                style={{ fontSize: 'var(--fs-base)' }} fill="var(--t-ink)">{formatDur(durSecs)}</text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
