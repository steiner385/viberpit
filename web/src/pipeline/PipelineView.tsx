// Ported from plotroom@5166b52 frontend/src/sections/pipeline/PipelineView.tsx; edits:
// import repoints (../../types → ./types, ../../PrRow → ./PrRow, ../../QueueTrain → ./QueueTrain,
// ../../StatusStrip → ./StatusStrip; ./ordering + ./queueFront unchanged); ViberPit blend (spec §6) —
// optional onFixPr? prop threaded to PrRow in the row() helper.
//
// Phase-2 blend edits (declared): (1) CONTROLLED FILTER — optional activeFilter?/onFilter?
// props let the host (PipelineTab) drive the status filter (e.g. HealthHeader lane-jump);
// internal state is used only when uncontrolled (activeFilter === undefined). (2) COLLAPSE
// PERSISTENCE — the collapsed-repo set persists to localStorage under storageKey('pipeline.collapsed')
// (migrated forward from the legacy 'prdash.collapsed' key, row 7b) via the private-mode-safe
// readCollapsedSet/writeCollapsedSet helpers copied verbatim from $PLOT/App.tsx:33-55. (3) KIOSK — no longer a prop here: StatusStrip and PrRow each read
// read-only-ness from shell/InteractionContext directly (the host wraps its tree in
// <InteractionProvider interactive={!kiosk}>), replacing the old kiosk?/interactive?/
// expandable? prop chain (the $PLOT/App.tsx:348,403 pattern).
//
// Pipeline section — the original PR pipeline view, ported into the unified
// workspace IA. Reuses the existing PrRow / QueueTrain / StatusStrip verbatim so
// the rich per-PR rows (stage, queue, checks, ready+auto-merge actions) and the
// status filter are identical to the classic dashboard; only the framing (focused
// repo first) is workspace-native. Data is the live Tier-1 state.
import { useState, useMemo } from 'react';
import { ArrowUpRight, ChevronDown, ChevronRight, Package, SkipForward } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Button } from '@/ui/Button';
import { TOUCH_FLOOR } from '@/ui/list/listControlStyles';
import type { DashboardState } from './types';
import { PrRow } from './PrRow';
import { QueueTrain } from '@/features/pipeline/charts/QueueTrain';
import { StatusStrip, bucketPr, isActivePr, isFailedPr, type Bucket } from './StatusStrip';
import { splitCohort, deployBreakdown, newestMergedAt } from './ordering';
import { formatSince } from './format';
import { nextToMerge } from './queueFront';
import { storageKey, migrateStorageKey, migrateStorageNamespace } from '@/lib/storageKey';

// ---- localStorage helpers (private-mode safe) — copied verbatim from $PLOT/App.tsx:33-55 ----

const LS_KEY = storageKey('pipeline.collapsed');
/** Pre-namespace key. Migrated forward on first read (row 7b). */
const LEGACY_LS_KEY = 'prdash.collapsed';

function readCollapsedSet(): Set<string> {
  try {
    migrateStorageNamespace(localStorage, LS_KEY);
    migrateStorageKey(localStorage, LEGACY_LS_KEY, LS_KEY);
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw) as unknown;
    if (Array.isArray(parsed)) return new Set(parsed as string[]);
  } catch {
    // invalid JSON or storage access denied — return empty set
  }
  return new Set();
}

function writeCollapsedSet(set: Set<string>): void {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify([...set]));
  } catch {
    // private-mode storage full — silently ignore
  }
}

/** The board's one-line status notes (deploy backlog, merges next, deploy
 *  chain): muted, small, and spaced like the rows they sit between. */
const NOTE = 'mb-1.5 flex items-center gap-1.5 text-xs text-ink-muted';
/** A note's leading drawn icon (was a colour emoji — 📦 ⏭ ⤴). */
const NOTE_ICON = 'size-3.5 shrink-0';
/** The fold toggles (repo, deploy cohort, merged ledger): 44px on a finger. */
const FOLD = cn('gap-1.5 px-1 text-xs text-ink-muted hover:text-ink', TOUCH_FLOOR);

function Fold({ open }: { open: boolean }) {
  const Icon = open ? ChevronDown : ChevronRight;
  return <Icon size={14} aria-hidden="true" className="shrink-0" />;
}

/** Compact ETA like "~5m" / "~1h"; null when unknown. */
function eta(secs: number | null): string | null {
  if (secs == null) return null;
  return secs < 90 ? `~${Math.round(secs)}s` : secs < 5400 ? `~${Math.round(secs / 60)}m` : `~${Math.round(secs / 3600)}h`;
}


export function PipelineView({ state, focusedRepo, onFixPr, activeFilter: controlledFilter, onFilter, portrait }: {
  /** Non-null: the page renders its own loading skeleton before the first
   *  frame and only mounts the board once a frame exists. */
  state: DashboardState;
  focusedRepo: string | null;
  onFixPr?: (sessionId: string, prNumber: number) => void;
  /** Controlled status filter: when provided (including null), the host owns the
   *  filter and internal state is bypassed. Omit for the standalone/uncontrolled use. */
  activeFilter?: Bucket | null;
  onFilter?: (bucket: Bucket | null) => void;
  /** `useViewport().portrait` (#1407, FB-2), threaded by the host rather than
   *  read here directly — `PipelineView` renders standalone in tests with no
   *  `ViewportProvider` in the tree. Defaults to landscape composition, same
   *  as before this prop existed. Drives `.pipeline-portrait` in plotroom.css,
   *  which lets the queue train wrap instead of relying on pointer-only
   *  horizontal scroll a kiosk viewer cannot use. */
  portrait?: boolean;
}) {
  const [ownFilter, setOwnFilter] = useState<Bucket | null>(null);
  const activeFilter = controlledFilter !== undefined ? controlledFilter : ownFilter;
  const setFilter = onFilter ?? setOwnFilter;
  const [collapsed, setCollapsed] = useState<Set<string>>(readCollapsedSet);
  const [openCohorts, setOpenCohorts] = useState<Set<string>>(new Set());
  const [openLedgers, setOpenLedgers] = useState<Set<string>>(new Set());

  // focused repo first (stable otherwise) — the workspace's "current pipeline" lead.
  // Memoized on [state, focusedRepo] so SSE frames that don't change these skip the sort.
  const repos = useMemo(
    () => [...state.repos].sort((a, b) => (a.repo === focusedRepo ? -1 : b.repo === focusedRepo ? 1 : 0)),
    [state, focusedRepo],
  );
  // Tile counts reflect the focused repo so the strip answers "what's happening
  // in the repo I'm looking at?" rather than a global cross-repo aggregate.
  const tileRepo = repos.find((r) => r.repo === focusedRepo) ?? repos[0];
  const allPrs = useMemo(() => tileRepo?.prs ?? [], [tileRepo]);

  const toggle = (repo: string) => setCollapsed((p) => { const n = new Set(p); n.has(repo) ? n.delete(repo) : n.add(repo); writeCollapsedSet(n); return n; }); // eslint-disable-line @typescript-eslint/no-unused-expressions -- ternary-as-statement, verbatim from the plotroom source (port fidelity)
  const toggleCohort = (repo: string) => setOpenCohorts((p) => { const n = new Set(p); n.has(repo) ? n.delete(repo) : n.add(repo); return n; }); // eslint-disable-line @typescript-eslint/no-unused-expressions -- ternary-as-statement, verbatim from the plotroom source (port fidelity)
  const toggleLedger = (repo: string) => setOpenLedgers((p) => { const n = new Set(p); if (n.has(repo)) n.delete(repo); else n.add(repo); return n; });
  const row = (pr: typeof allPrs[number], r: typeof repos[number], live = false) => (
    <PrRow key={pr.number} pr={pr} hasDeploy={r.hasDeploy} queueCulprit={r.queue?.unmergeableCulprit ?? null} onFixPr={onFixPr} live={live} />
  );

  return (
    // The Workspace variant mounts this pane under a `scroll={false}`
    // PageContent, so the pane itself is what scrolls.
    <div className={`flex h-full min-h-0 flex-col overflow-y-auto px-4 py-3 md:px-6${portrait ? ' pipeline-portrait' : ''}`}>
      <h2 className="sr-only">Pipeline</h2>
      {/* The header's lanes count every repo; with more than one on the board the
          strip names the one its tiles count, so "4 red" beside "3 Failed" reads. */}
      <StatusStrip prs={allPrs} activeFilter={activeFilter} onFilter={setFilter}
        scope={repos.length > 1 ? tileRepo?.repo : undefined} />
      {repos.map((r) => {
        const isCollapsed = collapsed.has(r.repo);
        const visiblePrs = activeFilter ? r.prs.filter((pr) => bucketPr(pr) === activeFilter) : r.prs;
        const hiddenCount = r.prs.length - visiblePrs.length;
        const activeCount = r.prs.filter(isActivePr).length;
        const failedCount = r.prs.filter(isFailedPr).length;
        return (
          <section key={r.repo}>
            {/* The heading exists for SR heading navigation (UX-L1); the button
                inside it carries the styling and the collapse affordance. */}
            <h3 className="mt-4 mb-2 border-b border-edge font-normal">
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className={cn('w-full justify-start gap-1.5 px-0 text-ink-muted hover:bg-transparent hover:text-ink', TOUCH_FLOOR)}
                aria-expanded={!isCollapsed}
                onClick={() => toggle(r.repo)}
              >
                <Fold open={!isCollapsed} />
                {r.repo}
                {!isCollapsed && hiddenCount > 0 && <span className="text-xs font-normal text-ink-muted"> ({hiddenCount} hidden)</span>}
                {isCollapsed && (
                  <span className="text-xs font-normal text-ink-muted">
                    <span>{r.prs.length} PRs</span>
                    {activeCount > 0 && <span> · {activeCount} active</span>}
                    {failedCount > 0 && <span className="text-danger"> · {failedCount} failed</span>}
                  </span>
                )}
              </Button>
            </h3>
            {!isCollapsed && (() => {
              const split = splitCohort(visiblePrs);
              // The merged ledger folds to one line unless the operator asked
              // for exactly those rows (the Merged / parked tile), in which case
              // they ARE the lead. Overdue merged rows never fold (`inLedger`).
              const ledgerFolded = activeFilter !== 'merged';
              const lead = ledgerFolded ? split.lead : [...split.lead, ...split.merged];
              const ledger = ledgerFolded ? split.merged : [];
              const { cohort } = split;
              const cohortOpen = openCohorts.has(r.repo);
              const ledgerOpen = openLedgers.has(r.repo);
              const newest = newestMergedAt(ledger);
              // Deploy backlog: QA/prod counted from the rows' own deploy stages —
              // the server derives qa-deploy/awaiting-prod by the same rule as the
              // deploy block / deployBacklog, and counting the rows here keeps this
              // line equal to the Deploying tile (it used to read deployBacklog
              // while the tile read stages, and the two disagreed). Admin has no
              // stage, so it still comes from GitHub-Deployments.
              const backlog = (r.deployBacklog || r.deploy)
                ? { ...deployBreakdown(r.prs), awaitingAdmin: r.deployBacklog?.awaitingAdmin ?? 0 }
                : null;
              const next = nextToMerge(r.queue);
              const liveSet = new Set(next?.prNumbers ?? []);
              // The notes are role="note", not "region": a region is a landmark,
              // and three per repo flooded the landmark list with up to 21 entries.
              return (
                <>
                  <QueueTrain repo={r.repo} queue={r.queue} />
                  {backlog && (backlog.awaitingQa > 0 || backlog.awaitingProd > 0 || backlog.awaitingAdmin > 0) && (
                    <p className={NOTE} role="note" aria-label="Deploy backlog">
                      <Package className={NOTE_ICON} aria-hidden="true" />
                      <span>
                        Deploy backlog:{' '}
                        {[
                          backlog.awaitingQa > 0 ? `${backlog.awaitingQa} awaiting QA` : null,
                          backlog.awaitingProd > 0 ? `${backlog.awaitingProd} awaiting prod` : null,
                          backlog.awaitingAdmin > 0 ? `${backlog.awaitingAdmin} awaiting admin` : null,
                        ].filter(Boolean).join(' · ')}
                      </span>
                    </p>
                  )}
                  {next && (() => {
                    const prs = next.prNumbers.map((n) => `#${n}`).join(', ');
                    const e = eta(next.etaSeconds);
                    return (
                      <p className={NOTE} role="note" aria-label="Merges next">
                        <SkipForward className={NOTE_ICON} aria-hidden="true" />
                        <span>
                          Merges next: <strong className="font-semibold text-ink">{prs}</strong>{' '}
                          {next.building ? `building${next.percent != null ? ` ${next.percent}%` : ''}` : 'front of queue'}
                          {e ? ` · ${e}` : ''}
                        </span>
                      </p>
                    );
                  })()}
                  {visiblePrs.length === 0 && hiddenCount === 0 && <p className="text-xs text-ink-muted">no active PRs</p>}
                  {lead.map((pr) => row(pr, r, liveSet.has(pr.number)))}
                  {cohort.length > 0 && (() => {
                    // Disjoint deploy stages — never lump awaiting-QA into "awaiting prod".
                    const { awaitingQa, awaitingProd } = deployBreakdown(cohort);
                    const parts = [
                      awaitingQa > 0 ? `${awaitingQa} awaiting QA` : null,
                      awaitingProd > 0 ? `${awaitingProd} awaiting prod` : null,
                    ].filter(Boolean);
                    const label = parts.length ? parts.join(' · ') : 'deploying';
                    return (
                      <div className="mb-1.5">
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          className={FOLD}
                          aria-expanded={cohortOpen}
                          onClick={() => toggleCohort(r.repo)}
                        >
                          <Fold open={cohortOpen} /> {cohort.length} merged · {label}
                        </Button>
                        {cohortOpen && cohort.map((pr) => row(pr, r))}
                      </div>
                    );
                  })()}
                  {/* The merged ledger (2026-09-30 Pipeline critique, P1): one
                      line per repo for the week's landed PRs, the rows one tap
                      away — same idiom as the deploy cohort above. Closed by
                      default, so the kiosk's band and a phone's first fold hold
                      live work, not history. */}
                  {ledger.length > 0 && (
                    <div className="mb-1.5">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className={FOLD}
                        aria-expanded={ledgerOpen}
                        onClick={() => toggleLedger(r.repo)}
                      >
                        <Fold open={ledgerOpen} />
                        {' '}{ledger.length} merged in the last 7 days{newest ? ` · last ${formatSince(newest)}` : ''}
                      </Button>
                      {ledgerOpen && ledger.map((pr) => row(pr, r))}
                    </div>
                  )}
                  {r.deploy?.chain && (r.deploy.chain.inFlight || r.deploy.chain.supersededCount > 0) && (
                    <p className={NOTE} role="note" aria-label="Deploy chain">
                      <ArrowUpRight className={NOTE_ICON} aria-hidden="true" />
                      <span>
                        {r.deploy.chain.inFlight && (
                          <>Deploying <strong className="font-semibold text-ink">#{r.deploy.chain.inFlight.prNumber}</strong> — at {r.deploy.chain.inFlight.stage}, flowing to prod</>
                        )}
                        {r.deploy.chain.supersededCount > 0 && (
                          <span>
                            {r.deploy.chain.inFlight ? ' · ' : ''}
                            {r.deploy.chain.supersededCount} superseded (rolled into a newer deploy)
                          </span>
                        )}
                      </span>
                    </p>
                  )}
                </>
              );
            })()}
          </section>
        );
      })}
    </div>
  );
}
