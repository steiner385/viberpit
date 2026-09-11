// Ported from plotroom@5166b52 frontend/src/LegendPanel.tsx; edits: migrated onto the
// shared `Sheet` primitive (portal/trap/backdrop), which now owns focus trapping —
// the direct `useFocusTrap` call this component used to make is gone.
import type { ReactNode, RefObject } from 'react';
import { TILE_DEFINITIONS } from './StatusStrip';
import {
  METRIC_DEFINITIONS, SUBLINE_TERMS, LANE_STATE_DEFINITIONS, LANE_DEFINITIONS,
  SETTINGS_DEFINITIONS, DESIGNER_DEFINITIONS, METRIC_SECTION_GROUPS,
} from './definitions';
import { Sheet } from '@/ui/Sheet';
import { TONE_FILL } from '@/ui/Badge';
import { cn } from '@/lib/utils';

/** One definition-list row: a term column with a 110 px (6.875 rem) floor so
 *  the descriptions line up across rows — each row is its own grid, so without
 *  the floor every row would size its term column to its own label. */
const LEGEND_DL_ROW = 'my-1.5 grid grid-cols-[minmax(6.875rem,auto)_1fr] gap-x-3 gap-y-0.5 text-xs '
  + '[&>dd]:m-0 [&>dd]:text-ink-muted [&>dt]:flex [&>dt]:items-center [&>dt]:gap-1 [&>dt]:font-mono [&>dt]:text-2xs [&>dt]:text-ink';

interface LegendPanelProps {
  open: boolean;
  onClose: () => void;
  /** Element to return focus to on close (the `?` header button). */
  returnFocusRef?: RefObject<HTMLElement | null>;
}

/** A node circle exactly as the live MetroTrack renders it (same classes). */
function ExampleNode({ status, glyph }: { status: string; glyph: string }) {
  return (
    <span className={`node ${status}`}>
      <span className="c" aria-hidden="true">{glyph}</span>
    </span>
  );
}

/** The example slot carries `pipeline-chart`, the chart stylesheet's scope root
 *  — the swatches inside it (`.node`, `.seg`) are the live chart classes and
 *  render unstyled without it. It deliberately does NOT carry `track`: the
 *  root class is what gates the active-node pulse, so a legend swatch stays
 *  static, as a swatch should. */
function LegendRow({ example, children }: { example: ReactNode; children: ReactNode }) {
  return (
    <div className="my-2 flex items-center gap-3">
      <span className="pipeline-chart legend-ex flex min-w-8 shrink-0 items-center justify-center">{example}</span>
      <span className="text-xs text-ink-muted [&_b]:font-semibold [&_b]:text-ink [&_code]:text-2xs [&_code]:text-ink">{children}</span>
    </div>
  );
}

/** Mini queue-train car reusing the live `.car` classes. */
function ExampleCar({ cls, header, body }: { cls: string; header: string; body: string }) {
  return (
    <div className={`car ${cls}`}>
      <div className="car-header">{header}</div>
      <span className="car-numbers">{body}</span>
    </div>
  );
}

/** Mini Gantt bar row reusing the live `.g-row` / `.g-bar` classes. */
function ExampleBar({ kind, name, fillPct, time, band, tick, nameTitle }: {
  kind: string; name: string; fillPct: number; time: string;
  band?: [number, number]; tick?: number; nameTitle?: string;
}) {
  return (
    <li className={`g-row ${kind}`}>
      <span className="g-name" title={nameTitle}>{name}</span>
      <span className="g-bar">
        {band && <span className="band" style={{ left: `${band[0]}%`, width: `${band[1] - band[0]}%` }} />}
        <i style={{ width: `${fillPct}%` }} />
        {tick != null && <span className="exp" style={{ left: `${tick}%` }} />}
      </span>
      <span className="g-t">{time}</span>
    </li>
  );
}

// Sub-line terms + metric definitions are shared with the in-place tooltips
// (issue #66) via definitions.ts — the legend is the deep-dive view of the
// SAME copy, so the two layers can't drift.

export function LegendPanel({ open, onClose, returnFocusRef }: LegendPanelProps) {
  return (
    <Sheet open={open} onClose={onClose} title="Legend" side="right" width={420} returnFocusRef={returnFocusRef}>
      <div className="space-y-5 px-4 pb-6 pt-3">
        {/* Delivery spine — the 5-state lane vocabulary (spec §16); the
              glyphs mirror the live rail rendering exactly. */}
          <section className="space-y-2">
            <h3 className="text-2xs font-semibold uppercase tracking-wider text-ink-muted">Delivery spine</h3>
            <p className="text-2xs leading-relaxed text-ink-muted [&_b]:text-ink [&_code]:text-2xs [&_code]:text-ink">
              The lifecycle rail colors the <b>glyph</b> only; lane text stays in the
              default color. Worst lane wins the rollup pill at the top.
            </p>
            <dl className="m-0">
              {Object.entries(LANE_STATE_DEFINITIONS).map(([key, def]) => (
                <div className={LEGEND_DL_ROW} key={key}>
                  <dt>{def.label}</dt>
                  <dd>{def.text}</dd>
                </div>
              ))}
            </dl>
            <p className="text-2xs leading-relaxed text-ink-muted [&_b]:text-ink [&_code]:text-2xs [&_code]:text-ink">
              The rail has one lane per delivery stage — what each lane covers:
            </p>
            <dl className="m-0">
              {Object.entries(LANE_DEFINITIONS).map(([key, def]) => (
                <div className={LEGEND_DL_ROW} key={key}>
                  <dt>{def.label}</dt>
                  <dd>{def.text}</dd>
                </div>
              ))}
            </dl>
          </section>

          {/* (a) Pipeline track */}
          <section className="space-y-2">
            <h3 className="text-2xs font-semibold uppercase tracking-wider text-ink-muted">Pipeline track</h3>
            <LegendRow example={<ExampleNode status="done" glyph="✓" />}>
              <b>Done</b> — stage complete
            </LegendRow>
            <LegendRow example={<ExampleNode status="active" glyph="2" />}>
              <b>Active</b> — current stage in progress (pulses; shows its label and <code>~ETA</code> underneath)
            </LegendRow>
            <LegendRow example={<ExampleNode status="fail" glyph="✗" />}>
              <b>Failed</b> — CI failed (at the CI node) or the merge-group build failed (at the Queue node)
            </LegendRow>
            <LegendRow example={<ExampleNode status="pending" glyph="3" />}>
              <b>Pending</b> — stage not reached yet (numbered)
            </LegendRow>
            <LegendRow example={<ExampleNode status="parked" glyph="!" />}>
              <b>Parked</b> — needs attention before it can advance: draft, conflicting,
              unmergeable, or blocked behind a queue conflict (the node label names the reason)
            </LegendRow>
            <LegendRow example={
              <span className="legend-track-segs flex items-center">
                <i className="seg done" />
                <i className="seg part"
                  style={{ background: 'linear-gradient(90deg, var(--t-accent) 60%, var(--t-edge) 60%)' }} />
                <i className="seg" />
              </span>
            }>
              <b>Segments</b> — green: stage behind it is complete; partial blue fill:
              progress % of the active stage; gray: not reached
            </LegendRow>
          </section>

          {/* (b) Row sub-line terms */}
          <section className="space-y-2">
            <h3 className="text-2xs font-semibold uppercase tracking-wider text-ink-muted">Row sub-line terms</h3>
            <dl className="m-0">
              {SUBLINE_TERMS.map(({ term, text }) => (
                <div className={LEGEND_DL_ROW} key={term}>
                  <dt>{term}</dt>
                  <dd>{text}</dd>
                </div>
              ))}
            </dl>
          </section>

          {/* (c) Expanded job bars */}
          <section className="space-y-2">
            <h3 className="text-2xs font-semibold uppercase tracking-wider text-ink-muted">Expanded job bars</h3>
            <ul className="pipeline-chart checks gantt legend-gantt">
              <ExampleBar kind="g-done" name="passed" fillPct={70} time="4m ✓" />
              <ExampleBar kind="g-running" name="running" fillPct={45} time="2m / ~5m"
                band={[40, 75]} tick={55} />
              <ExampleBar kind="g-overdue" name="running long" fillPct={95} time="9m ⚠ overdue" />
              <ExampleBar kind="g-failed" name="failed" fillPct={35} time="1m ✗" />
              <ExampleBar kind="g-queued" name="queued" fillPct={15} time="—" />
              <ExampleBar kind="g-queued g-runner-wait" name="runner wait" fillPct={15}
                time="⧗ waiting for runner" />
              <ExampleBar kind="g-queued g-runner-wait g-runner-wait-amber" name="slow runner wait"
                fillPct={15} time="⧗ 2× typical" />
              <ExampleBar kind="g-done" name="required job" fillPct={60} time="3m ✓" />
              <ExampleBar kind="g-done g-advisory" name="advisory job" fillPct={50} time="2m ✓"
                nameTitle="advisory — does not gate merging" />
            </ul>
            <p className="text-2xs leading-relaxed text-ink-muted [&_b]:text-ink [&_code]:text-2xs [&_code]:text-ink">
              Bar fill = elapsed time on a shared scale. Tinted <b>band</b> = the job&apos;s typical
              p10–p90 duration range; dark <b>tick</b> = its typical (p50) duration.
              Striped fill = waiting for a CI runner (amber once the wait exceeds 2× typical).
            </p>
            <p className="text-2xs leading-relaxed text-ink-muted [&_b]:text-ink [&_code]:text-2xs [&_code]:text-ink">
              <b>Required</b> job names render normally — a failure blocks merge.{' '}
              <em className="text-ink-muted">Advisory</em> job names are italic and muted — they
              run but do not gate merging (hover for tooltip).
            </p>
            <p className="text-2xs leading-relaxed text-ink-muted [&_b]:text-ink [&_code]:text-2xs [&_code]:text-ink">
              <code>⧗ waiting for runner</code> — queued, no runner assigned yet ·{' '}
              <code>⊘ blocked on X</code> — waiting for upstream job X to finish ·{' '}
              <code>–</code> — skipped
            </p>
            <p className="text-2xs leading-relaxed text-ink-muted [&_b]:text-ink [&_code]:text-2xs [&_code]:text-ink">
              <code>⚐ flakes N%</code> — this failing check has failed-then-passed on the
              same commit in N% of its recent runs (7 days) — likely a flake; consider a
              re-run before debugging. The row stays red: a flake rate is a probability,
              not a verdict.
            </p>
          </section>

          {/* (d) Queue train */}
          <section className="space-y-2">
            <h3 className="text-2xs font-semibold uppercase tracking-wider text-ink-muted">Queue train</h3>
            <p className="text-2xs leading-relaxed text-ink-muted [&_b]:text-ink [&_code]:text-2xs [&_code]:text-ink">One car per merge-queue grouping, front of the queue first.</p>
            <div className="pipeline-chart">
              <div className="queue-train legend-train">
                <ExampleCar cls="building" header="▶ group" body="#101 #102" />
                <ExampleCar cls="building failed" header="✗ failing" body="#103" />
                <ExampleCar cls="queued" header="next batch" body="#104 #105" />
                <ExampleCar cls="unmergeable" header="✗ unmergeable" body="#106" />
                <ExampleCar cls="queue-blocked" header="⊘ blocked behind conflict" body="#107" />
              </div>
            </div>
            <dl className="m-0">
              <div className={LEGEND_DL_ROW}>
                <dt>solid blue</dt>
                <dd>merge group building — speculative merge running the full CI suite (shows % and ETA)</dd>
              </div>
              <div className={LEGEND_DL_ROW}>
                <dt>solid red</dt>
                <dd>that group&apos;s build failed — the queue re-batches without the culprit</dd>
              </div>
              <div className={LEGEND_DL_ROW}>
                <dt>dashed gray</dt>
                <dd>waiting — &ldquo;next batch&rdquo; starts building when a slot frees; &ldquo;then&rdquo; collapses the rest</dd>
              </div>
              <div className={LEGEND_DL_ROW}>
                <dt>dashed red</dt>
                <dd>unmergeable — conflicts with the base branch; needs a rebase, facing ejection</dd>
              </div>
              <div className={LEGEND_DL_ROW}>
                <dt>dashed amber</dt>
                <dd>blocked behind a conflicting entry ahead — not conflicting itself; revalidates once the culprit is ejected</dd>
              </div>
            </dl>
          </section>

          {/* (e) Status tiles */}
          <section className="space-y-2">
            <h3 className="text-2xs font-semibold uppercase tracking-wider text-ink-muted">Status tiles</h3>
            <dl className="m-0">
              {TILE_DEFINITIONS.map(({ bucket, label, tone, title }) => (
                <div className={LEGEND_DL_ROW} key={bucket}>
                  <dt>
                    {/* The swatch reads the SAME TILE_TONE the board's tiles do, so
                        the legend cannot describe a colour the board no longer uses. */}
                    <span className={cn('legend-swatch inline-block size-3 shrink-0 rounded-sm', TONE_FILL[tone])} aria-hidden="true" /> {label}
                  </dt>
                  <dd>{title}</dd>
                </div>
              ))}
            </dl>
            <p className="text-2xs leading-relaxed text-ink-muted [&_b]:text-ink [&_code]:text-2xs [&_code]:text-ink">Click a tile to filter the board to that bucket; click again to clear.</p>
          </section>

          {/* (f) Settings sub-pages — the same per-page copy the Settings dialog
              shows on each section heading; the deep-dive lists all 5 together. */}
          <section className="space-y-2">
            <h3 className="text-2xs font-semibold uppercase tracking-wider text-ink-muted">Settings</h3>
            <p className="text-2xs leading-relaxed text-ink-muted [&_b]:text-ink [&_code]:text-2xs [&_code]:text-ink">
              What each section of the Settings dialog (⚙) controls:
            </p>
            <dl className="m-0">
              {Object.entries(SETTINGS_DEFINITIONS).map(([key, def]) => (
                <div className={LEGEND_DL_ROW} key={key}>
                  <dt>{def.label}</dt>
                  <dd>{def.text}</dd>
                </div>
              ))}
            </dl>
          </section>

          {/* (g) Designer (protection map) — the tab had no legend coverage; this
              explains the matrix, its cell states, drift, overlays, and findings. */}
          <section className="space-y-2">
            <h3 className="text-2xs font-semibold uppercase tracking-wider text-ink-muted">Designer (protection map)</h3>
            <dl className="m-0">
              {Object.entries(DESIGNER_DEFINITIONS).map(([key, def]) => (
                <div className={LEGEND_DL_ROW} key={key}>
                  <dt>{def.label}</dt>
                  <dd>{def.text}</dd>
                </div>
              ))}
            </dl>
          </section>

          {/* (h) Metrics & ops figures — the same copy the in-place tooltips
              carry (issue #66); this is the browsable deep-dive, grouped by the
              Metrics tab's own sub-pages instead of one flat list. */}
          <section className="space-y-2">
            <h3 className="text-2xs font-semibold uppercase tracking-wider text-ink-muted">Metrics &amp; ops figures</h3>
            <p className="text-2xs leading-relaxed text-ink-muted [&_b]:text-ink [&_code]:text-2xs [&_code]:text-ink">
              Every headline number also explains itself in place — hover it.
              The full vocabulary, by where it appears:
            </p>
            {METRIC_SECTION_GROUPS.map((group) => (
              <div className="mt-2.5" key={group.label}>
                <h4 className="mb-0.5 text-2xs font-semibold uppercase tracking-wider text-ink opacity-75">{group.label}</h4>
                <dl className="m-0">
                  {group.keys.map((key) => (
                    <div className={LEGEND_DL_ROW} key={key}>
                      <dt>{METRIC_DEFINITIONS[key].label}</dt>
                      <dd>{METRIC_DEFINITIONS[key].text}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            ))}
          </section>
      </div>
    </Sheet>
  );
}
