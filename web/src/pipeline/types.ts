// Ported from plotroom@5166b52 frontend/src/types.ts; edits: notification-events import → ./notification-events; removed Config API mirrors, Metrics API mirrors, Runner routing API mirrors; kept LaneStatus (DashboardState.laneHealth); moved LeadTimeSegmentId here from metrics block; added session? extension to PrView; restored LaneView and Lane interfaces (Task 1)
// Notification event-type registry — single shared source of truth (server +
// frontend); see shared/notification-events.ts. Imported for local use below and
// re-exported so existing frontend importers are unchanged.
import type { NotificationEventType, NotificationKind } from './notification-events';
export type { NotificationEventType, NotificationKind };

export type StageId = 'ci' | 'parked' | 'ready' | 'queue' | 'qa-deploy' | 'awaiting-prod' | 'merged';
export interface StageResult {
  stage: StageId; substate: string | null;
  percent: number | null; etaSeconds: number | null;
  etaRangeSeconds: [number, number] | null; overdue: boolean;
}
export interface CheckView {
  name: string; status: string; conclusion: string | null; isRequired: boolean;
  /** Workflow display name (e.g. `CI`, `Auto-merge PRs`); null when unknown. */
  workflowName: string | null;
  elapsedSeconds: number | null; expectedSeconds: number | null; url: string | null;
  /** Lower/upper expected-duration bounds (p10/p90 over the same last-20 SUCCESS
   *  window as expectedSeconds); null whenever expectedSeconds is null. */
  expectedLowSeconds: number | null; expectedHighSeconds: number | null;
  /** Queued-check runner classification (null for non-queued checks):
   *  'runner' = needs satisfied, waiting for a runner; 'blocked' = waiting on
   *  blockedOn; 'unknown' = needs graph unknown or root job. */
  waitKind: 'runner' | 'blocked' | 'unknown' | null;
  blockedOn: string | null;
  waitingSeconds: number | null;
  expectedRunnerWaitSeconds: number | null;
  /** Flake radar (issue #37): 7-day flake rate when the check has enough
   *  history (≥5 distinct (sha, attempt) samples); null otherwise. */
  flakeRatePct: number | null;
  /** True when the check is currently failing-class AND its flake rate ≥ 20% —
   *  the failure is likely a flake; consider a re-run. */
  likelyFlake: boolean;
  /** Duration regression (issue #41): true while the check's (name, event)
   *  series has an active p50 step-up — the Gantt's ↑ badge. Optional to
   *  tolerate pre-upgrade payloads. */
  regressed?: boolean;
  /** The step behind the badge (tooltip numbers); null/absent when not regressed. */
  regression?: DurationRegressionInfo | null;
  /** Spot-reclaim ledger (issue #46): a CANCELLED check whose sha has a newer
   *  attempt running/queued — render '↻ re-run in progress — likely spot
   *  reclaim, do nothing'. Optional to tolerate pre-upgrade payloads. */
  rerunInProgress?: boolean;
}

/** Mirror of server poller.ts DurationRegressionInfo (issue #41). */
export interface DurationRegressionInfo {
  priorP50Secs: number;
  recentP50Secs: number;
  ratio: number;
  /** ISO time of the recent window's first sample — the approximate onset. */
  sinceApprox: string;
}
/** Mirror of server poller.ts PrTimeline (issue #50): the merged_prs waterfall
 *  spine. A null waypoint was never observed — the segment is omitted. */
export interface PrTimeline {
  createdAt: string | null;
  firstGreenAt: string | null;
  enqueuedAt: string | null;
  mergedAt: string;
  qaLiveAt: string | null;
  prodLiveAt: string | null;
}

/** Mirror of server LEAD_TIME_SEGMENTS ids (issue #44), pipeline order. */
export type LeadTimeSegmentId =
  'toFirstGreen' | 'greenToEnqueued' | 'queue' | 'qaDeploy' | 'awaitingProd';

export interface PrView {
  repo: string; number: number; title: string; url: string;
  stage: StageResult;
  /** GitHub mergeStateStatus (…|DIRTY|CLEAN|UNKNOWN|DRAFT) for OPEN PRs; null for
   *  merged-PR views and pre-upgrade payloads. Gates the ready+auto-merge button:
   *  disabled when DIRTY (conflict). Optional to tolerate older payloads. */
  mergeStateStatus?: string | null;
  queueAheadCount: number | null;
  checks: CheckView[];
  /** Per-PR waterfall spine (issue #50) — merged PRs only; null for open PRs.
   *  Optional to tolerate pre-upgrade payloads. */
  timeline?: PrTimeline | null;
  /** Workflow-change flag (issue #49): the PR touches `.github/workflows/**` —
   *  render the '⚙ CI change' badge. Optional for pre-upgrade payloads. */
  touchesWorkflows?: boolean;
  /** Mirror of server workflow-impact.ts WorkflowImpact (issue #49): derived
   *  CI-graph diff summary lines vs main. Null/absent = no diff available
   *  (no graph, not computed yet) or no change. */
  workflowImpact?: { summary: string[] } | null;
  /** Queued PRs only: the merge-group build's checks (drives the queue stage ETA);
   *  null when not queued or the group rollup hasn't been fetched yet. */
  groupChecks: CheckView[] | null;
  /** Multi-train merge ETA simulation (issue #40) — WAITING queue entries only;
   *  null elsewhere (and on pre-upgrade payloads). */
  mergeEtaSim: MergeEtaSimulation | null;
  /** PR-level CI cost (cost explorer): elapsed runner-minutes of the CURRENT
   *  head's checks (running checks count started→now). Null when no check has
   *  started / merged PRs; optional to tolerate pre-upgrade payloads. */
  costMinutes?: number | null;
  /** The priced subset of costMinutes in dollars (poolMeta > costPerMinute >
   *  'default'); null in minutes-only mode. */
  costDollars?: number | null;
  /** True when costDollars is a known undercount: rates exist but at least
   *  one counted check ran on an unpriced pool — rendered '(partial)'. */
  costDollarsPartial?: boolean;
  /** ccswitch extension (spec §6): the authoring ccswitch session, if any. */
  session?: { id: string; name: string; fixable: boolean } | null;
}

/** Mirror of server estimator/queue.ts MergeEtaSimulation (issue #40). */
export interface MergeEtaSimulation {
  p50Secs: number;
  p90Secs: number;
  /** Trains that must complete before this PR merges. */
  trainsAhead: number;
  /** True when the p90 budgets one extra train for a likely eject. */
  assumesEjects: boolean;
}

/** Mirror of server estimator/queue-health.ts (issue #39). */
export type QueueHealthState = 'healthy' | 'cap-backlog' | 'dispatch-stall';
export interface QueueHealthView {
  state: QueueHealthState;
  /** Human remediation string ('dispatch-stall: … do NOT admin-merge', …). */
  detail: string;
  /** ISO time the queue entered this state. */
  since: string;
}
export interface QueueGroupView {
  oid: string;
  prNumbers: number[];
  percent: number | null;
  etaSeconds: number | null;
  failed: boolean;
}
export interface RepoQueueView {
  groups: QueueGroupView[];
  waiting: { prNumber: number; position: number; sim?: MergeEtaSimulation | null }[];
  /** PR numbers of GENUINELY conflicting UNMERGEABLE entries (DIRTY against the
   *  base — needs a rebase) — excluded from group coverage and waiting. */
  unmergeable: number[];
  /** Cascade-UNMERGEABLE entries: poisoned by a conflicting entry ahead, not
   *  conflicting with the base themselves — must not be told to rebase. */
  queueBlocked: number[];
  /** Lowest-position genuine conflict (presumed front-most UNMERGEABLE when no
   *  snapshot proves DIRTY); null without UNMERGEABLE entries. */
  unmergeableCulprit: number | null;
  batchSize: number;
  /** Ops console (issue #39) — optional only to tolerate pre-upgrade payloads. */
  health?: QueueHealthView;
  depth?: number;
  entriesWithWaitSecs?: { prNumber: number; position: number; waitSecs: number }[];
  trainsPerHour?: number;
  batchSuccessRatePct?: number | null;
  ejects24h?: number;
}

// ---- Delivery Spine (spec §2, §3.1, §4.2, §14) ----

/** The single public status vocabulary for Delivery-spine lanes (spec §2). */
export type LaneStatus = 'green' | 'amber' | 'red' | 'blind' | 'idle';

/** The serializable lane facts the server may compute and ship over SSE
 *  (spec §14: a positive allowlist — never tokens/errors/secrets). */
export interface LaneView {
  id: string;
  title: string;
  status: LaneStatus;
  summary: string;            // ≤200 chars, truncated '…'
  costChip?: { dollars: number; days: number } | null;
  efficiencyChip?: string | null;
  wiredness: 'wired' | 'not-wired';
  gating: boolean;
}

/** A fully-realized lane: a LaneView plus the client-only render function for
 *  its expanded panel (spec §3.1 — a render FUNCTION so it re-renders on SSE). */
export interface Lane extends LaneView {
  glyphPosition: 'dot' | 'crosscut';
  renderExpanded: () => import('react').ReactNode;
}

export interface DashboardState {
  generatedAt: string; staleSince: string | null;
  repos: { repo: string; hasDeploy: boolean; prs: PrView[]; queue: RepoQueueView | null;
    laneHealth?: { main: LaneStatus; lastGreenSha?: string | null; lastGreenAt?: string | null; mainSeries?: { ok: boolean | null }[] };
    /** Advisory Deploy-lane snapshot (Spec 2): per-env live commit sha (from
     *  /health) + awaiting-QA/awaiting-prod drift. Absent for repos with no
     *  deploy config. Mirror of server estimator/deploy-status.ts. */
    deploy?: { envs: { name: string; liveSha: string | null; reachable: boolean }[];
      awaitingQa: number; awaitingProd: number;
      /** QA→prod chain with SHA supersession (roadmap 4.4c). */
      chain?: { entries: { prNumber: number; sha: string | null; mergedAt: string;
        stage: 'merged' | 'qa' | 'prod'; qaLiveAt: string | null; prodLiveAt: string | null; superseded: boolean }[];
        inFlight: { prNumber: number; sha: string | null; stage: string } | null; supersededCount: number } };
    /** Advisory Scheduled-lane snapshot (Spec 4): the newest run per
     *  cron-scheduled workflow + the discovered-workflow count. Absent for
     *  repos with no scheduled workflows. Mirror of server poller.ts
     *  RepoScheduledStatus. Optional to tolerate pre-upgrade payloads. */
    scheduled?: { runs: { workflow: string; conclusion: string | null;
      status: string | null; createdAt: string | null; htmlUrl: string | null }[];
      discovered: number };
    /** Advisory Failures & flake-lane snapshot (Spec 5): the top flaky checks
     *  (≥ FLAKE_MIN_RUNS runs with same-sha fail-then-pass events) over a 14-day
     *  window + the flaky-check count. Absent for repos with no flaky checks.
     *  Mirror of server estimator/flake-summary.ts RepoFlakeSummary. Optional to
     *  tolerate pre-upgrade payloads. */
    flake?: { topChecks: { name: string; event: string; flakeRatePct: number; flakeEvents: number }[];
      flakyCount: number };
    /** Per-repo deploy backlog (flat 3-way: qa / prod / admin) derived from the
     *  GitHub-Deployments `deployed_envs` of merged PRs — no deploy-config
     *  required. Absent when the repo has no classifiable deploy env. Drives the
     *  "📦 Deploy backlog" line. Mirror of server deploy_status.backlog_from_prs. */
    deployBacklog?: { awaitingQa: number; awaitingProd: number; awaitingAdmin: number }; }[];
  /** Cross-cutting global CI cost summary (Cost lane, Spec 3) — top-level, not
   *  per-repo. Priced runner-minutes over a 7-day window across all repos,
   *  split by pipeline stage. `dollars` is null for an unpriced stage subset;
   *  `totalDollars`/`retryWastePct` are null in minutes-only mode (no rate
   *  configured) — never a fabricated $0. Mirror of server metrics.ts
   *  CostSummary. Optional to tolerate pre-upgrade payloads. */
  cost?: {
    totalDollars: number | null;
    days: number;
    byStage: { stage: 'pr' | 'queue' | 'main' | 'scheduled'; dollars: number | null; minutes: number }[];
    retryWastePct: number | null;
  };
}

// ---- Notifications (issue #19) ----
// Mirrors of server/notifier.ts — the payload of the named `notification` SSE
// event and the file-only `notifications` config block (read-only in the UI).
// (The event-type registry is imported from shared/ at the top of this file.)

export interface NotificationEvent {
  repo: string;
  prNumber: number;
  /** PR title. For 'digest': the pre-rendered subject line (repo is ''). */
  title: string;
  type: NotificationKind;
  detail: string;
  /** Server-rendered display strings — the single source of truth for what the
   *  bell shows (server/notifier.ts renderNotification). The browser displays
   *  these verbatim and never re-derives labels/subjects. */
  rendered?: { title: string; body: string };
}

export interface NotificationsConfig {
  enabled: boolean;
  command: string[];
  /** Masked by the server to scheme+host — the path may carry a token. */
  webhookUrl?: string;
  digest: { enabled: boolean; hourLocal: number };
  events: Record<NotificationEventType, boolean>;
}
