/**
 * Release-milestones API client (spec 2026-11-05 §9–§10): fetchers, query key and React
 * Query hooks. Kept out of `hooks.ts` so they ride the lazy Roadmap chunk, not the
 * startup bundle (bundle budget; same precedent as `digest.ts`). The types mirror the server's camelCase
 * view model verbatim (`server/pm/milestone_view.py:build_milestones_view`,
 * served by `server/api/pm_milestones.py`).
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, apiErrorCode, ApiRequestError } from './client'
import { PM_MUTATION_META, QK } from './hooks'

/** Read-time status (`server/pm/milestone_status.py`); `info` is a number with no target. */
export type ConditionStatus = 'met' | 'unmet' | 'unknown' | 'info'
export type ConditionOp = 'eq' | 'ne' | 'lt' | 'le' | 'gt' | 'ge'
export type MilestoneState = 'current' | 'done' | 'upcoming'
/** `server/pm/milestone_view.py:LEAF_STATES`. */
export type LeafState = 'done' | 'in_flight' | 'not_started'

/** A stored reading: a count, closed-of-total, or a manual tick. */
export type ConditionValue =
  | { n: number }
  | { done: number; total: number }
  | { met: boolean }
  | Record<string, unknown>

export interface MilestoneCondition {
  key: string
  label: string
  kind: string
  status: ConditionStatus
  value: ConditionValue | null
  target: number | null
  op: ConditionOp | null
  measuredAt: string | null
  ageSeconds: number | null
  error: string | null
  sourceUrl: string | null
  manualBy: string | null
  manualEvidence: string | null
  /** The tick's optional evidence link. Untrusted: render through `isSafeHttpUrl`. */
  manualUrl: string | null
}

export interface MilestonePlatform { name: string | null; off: boolean; note: string | null }
export interface MilestoneEpic { ref: string; url: string }
export interface LeafPr { number: number | null; url: string | null }

export interface MilestoneLeaf {
  ref: string
  title: string | null
  url: string | null
  state: LeafState
  prs: LeafPr[]
}

export interface MilestoneLeaves {
  done: number
  inFlight: number
  notStarted: number
  total: number
  measuredAt: string | null
  items: MilestoneLeaf[]
}

export interface GateSummary { met: number; total: number; unknown: number; ready: boolean }

/** An owner override recorded when a milestone advanced past an unmet gate (spec §7). */
export interface MilestoneExitOverride {
  by: string
  reason: string
  at: string
  unmet: { key: string; status: string; value: unknown }[]
}

export interface Milestone {
  key: string
  label: string | null
  name: string | null
  audience: string | null
  platforms: MilestonePlatform[]
  state: MilestoneState | null
  enteredAt: string | null
  exitedAt: string | null
  statusNote: string | null
  exitOverride: MilestoneExitOverride | null
  epics: MilestoneEpic[]
  gate: MilestoneCondition[]
  gateSummary: GateSummary
  leaves: MilestoneLeaves
}

export interface MilestoneLadder {
  title: string | null
  specPath: string | null
  specUrl: string | null
  sourcePath: string | null
  sourceSha: string | null
  syncedAt: string | null
  parseError: string | null
  parseErrorSha: string | null
  currentKey: string | null
  currentRemoved: boolean
  updatedAt: string | null
  autoRefresh: boolean
  /** No current milestone and at least one done: nothing left to advance. */
  ladderComplete: boolean
  /** Open owner asks on the whole ladder (the header strip's "N asks need you"). */
  asksOpen: number
}

/** `server/pm/milestone_asks.py:NOTE_KINDS`, `DECISION_STATUSES`, `ASK_STATUSES`. */
export type NoteKind = 'decision' | 'ask'
export type DecisionStatus = 'locked' | 'proposed' | 'superseded'
export type AskStatus = 'open' | 'done' | 'dropped'

/** An owner decision or ask (spec M11), ladder-wide when `milestoneKey` is null.
 *  `milestoneKey` may name a removed rung: the owner still resolves it. */
export interface MilestoneNote {
  id: string
  kind: NoteKind
  ref: string | null
  text: string
  url: string | null
  status: DecisionStatus | AskStatus
  milestoneKey: string | null
  createdBy: string | null
  createdAt: string | null
  resolvedBy: string | null
  resolvedAt: string | null
}

/** Server order: decisions locked, proposed, superseded; asks open first; each oldest first. */
export interface MilestoneNotes { decisions: MilestoneNote[]; asks: MilestoneNote[] }

export type MilestonesView =
  | { ladder: null }
  | { ladder: MilestoneLadder; milestones: Milestone[]; numbers: MilestoneCondition[]; notes: MilestoneNotes }

/** `POST …/refresh`: 202 stamped, 429 inside the window, 409 automatic refresh off. */
export type RefreshMilestonesResult =
  | { refreshed: true }
  | { refreshed: false; retryAfter: number }
  | { refreshed: false; reason: 'automatic_refresh_off' }

const base = (slug: string) => `/pm/projects/${encodeURIComponent(slug)}/milestones`

export function fetchMilestones(slug: string): Promise<MilestonesView> {
  return api.get<MilestonesView>(base(slug))
}

/** The 429 and 409 answers are outcomes the button renders, not failures. */
export async function refreshMilestones(slug: string): Promise<RefreshMilestonesResult> {
  try {
    return await api.post<RefreshMilestonesResult>(`${base(slug)}/refresh`)
  } catch (err) {
    if (err instanceof ApiRequestError && (err.status === 429 || err.status === 409)) {
      return err.body as RefreshMilestonesResult
    }
    throw err
  }
}

/** The scope a numbers-panel entry is ticked under; mirrors
 *  `server/pm/milestone_actions.py:NUMBERS_SCOPE`. */
export const NUMBERS_SCOPE = 'numbers'
/** Mirrors `milestone_actions.STATUS_NOTE_MAX`, `NOTE_TEXT_MAX`, `NOTE_REF_MAX`, `OVERRIDE_REASON_MIN`.
 *  A tick's evidence is capped at `NOTE_TEXT_MAX` too. */
export const STATUS_NOTE_MAX = 200
export const NOTE_TEXT_MAX = 500
export const NOTE_REF_MAX = 20
export const OVERRIDE_REASON_MIN = 10

/** A manual tick: `met` for a gate condition, `value` for a numbers entry. */
export interface TickVars {
  scope: string
  conditionKey: string
  met?: boolean
  value?: number
  evidence: string
  url?: string | null
}
export interface StatusNoteVars { key: string; statusNote: string | null }
export interface AddNoteVars {
  kind: NoteKind
  text: string
  ref?: string | null
  url?: string | null
  milestoneKey?: string | null
  status?: DecisionStatus | AskStatus
}
export interface UpdateNoteVars { id: string; status?: DecisionStatus | AskStatus; text?: string }
/** `expectedCurrent` null starts a ladder with no current milestone. */
export interface AdvanceVars { expectedCurrent: string | null; overrideReason?: string }

const enc = encodeURIComponent

// Every write answers with the full view (the same body as GET).
export function tickCondition(slug: string, { scope, conditionKey, ...body }: TickVars): Promise<MilestonesView> {
  return api.put<MilestonesView>(`${base(slug)}/${enc(scope)}/conditions/${enc(conditionKey)}`, body)
}
export function setStatusNote(slug: string, { key, statusNote }: StatusNoteVars): Promise<MilestonesView> {
  return api.patch<MilestonesView>(`${base(slug)}/${enc(key)}`, { statusNote })
}
export function addNote(slug: string, body: AddNoteVars): Promise<MilestonesView> {
  return api.post<MilestonesView>(`${base(slug)}/notes`, body)
}
export function updateNote(slug: string, { id, ...body }: UpdateNoteVars): Promise<MilestonesView> {
  return api.patch<MilestonesView>(`${base(slug)}/notes/${enc(id)}`, body)
}
export function advanceMilestone(slug: string, body: AdvanceVars): Promise<MilestonesView> {
  return api.post<MilestonesView>(`${base(slug)}/advance`, body)
}

export const MILESTONE_ERROR_FALLBACK = 'That change did not go through. Try again.'

/** THE copy for every code `server/pm/milestone_actions.py` raises (plus the route's
 *  `no_ladder`). Plain words for the person at the page, never the code itself. */
const MILESTONE_ERROR_COPY: Record<string, string> = {
  stale_current: 'Someone else advanced this ladder',
  gate_unmet: 'The gate is not met yet. Give a reason to advance with an override.',
  override_requires_owner: 'Only the owner can override from the ViberPit app',
  override_not_needed: 'The gate is met now, so no override is needed. Advance without a reason.',
  reason_too_short: `Give a reason of at least ${OVERRIDE_REASON_MIN} characters.`,
  evidence_required: 'Add evidence for this tick.',
  owner_required: 'Only the owner can change what someone else recorded.',
  person_required: 'Only a person can make this change, not an automation.',
  person_or_session_required: 'Only a person or a session can write this, not an automation.',
  ladder_complete: 'This ladder is complete. There is nothing left to advance.',
  no_milestones: 'This ladder has no milestones to start.',
  not_manual: 'This condition is measured automatically, so it cannot be ticked.',
  bad_tick: 'Choose Met or Not met, or enter a number.',
  too_long: 'That text is too long. Shorten it and try again.',
  text_required: 'Write the note before saving it.',
  bad_kind: 'Choose whether this is a decision or an ask.',
  bad_status: 'That status does not apply to this kind of note.',
  unknown_milestone: 'That milestone is no longer on the ladder. Reload the page.',
  unknown_condition: 'That condition is no longer in the definition file. Reload the page.',
  unknown_note: 'That note no longer exists. Reload the page.',
  nothing_to_update: 'There was nothing to change.',
  no_ladder: 'This product has no milestone ladder.',
  bad_url: 'A link must be a full web address that starts with http:// or https://.',
  concurrent_update: 'Someone else just changed this. Reload and try again.',
}

export function milestoneErrorCopy(code: string | null): string {
  return (code !== null && Object.hasOwn(MILESTONE_ERROR_COPY, code)) ? MILESTONE_ERROR_COPY[code] : MILESTONE_ERROR_FALLBACK
}

/** A failed write's words, or null when there is no failure. */
export function milestoneErrorText(err: unknown): string | null {
  return err ? milestoneErrorCopy(apiErrorCode(err)) : null
}

/** The one definition of the milestones query key. */
export const milestonesKey = (slug: string) => ['pm', 'milestones', slug] as const

/** A product's release-milestone ladder (spec 2026-11-05 §9). Polled every 60 s:
 *  the daemon refreshes measurements on its own cadence, and the page's
 *  "Updated N ago" must not freeze while it is open. Shared by the Milestones
 *  page and the product Overview's summary line, so the two are one fetch. */
export function useMilestones(slug: string | null) {
  return useQuery<MilestonesView>({
    queryKey: milestonesKey(slug ?? ''),
    queryFn: () => fetchMilestones(slug as string),
    enabled: slug !== null && slug !== '',
    staleTime: 10_000,
    refetchInterval: 60_000,
  })
}

/** Ask the daemon's Milestones Refresh loop to re-measure now. A 202 is only a
 *  request (the loop picks it up within a tick), so the view is invalidated to
 *  pull the fresh stamp; a 429/409 resolves as an outcome the button renders. */
export function useRefreshMilestones(slug: string) {
  const qc = useQueryClient()
  return useMutation<RefreshMilestonesResult, ApiRequestError, void>({
    mutationFn: () => refreshMilestones(slug),
    onSuccess: (r) => {
      if (r.refreshed) void qc.invalidateQueries({ queryKey: milestonesKey(slug) })
    },
    meta: PM_MUTATION_META,
  })
}

/** The refusals that mean the view under the caller is out of date: the ladder
 *  moved (`stale_current`), or the gate did (`gate_unmet`, `override_not_needed`),
 *  so the Advance dialog must switch mode from fresh props. */
const REFETCH_ON: ReadonlySet<string> = new Set(['stale_current', 'gate_unmet', 'override_not_needed'])

/** THE write hook: the answer replaces the cached view (no refetch). The map's
 *  needs-you badge counts open asks, so it is marked stale; it refetches only
 *  where it is on screen. A refusal in `REFETCH_ON` refetches the view to show
 *  where the ladder is now. */
function useMilestoneWrite<V>(slug: string, write: (slug: string, vars: V) => Promise<MilestonesView>) {
  const qc = useQueryClient()
  return useMutation<MilestonesView, ApiRequestError, V>({
    mutationFn: (vars) => write(slug, vars),
    onSuccess: (view) => {
      qc.setQueryData(milestonesKey(slug), view)
      void qc.invalidateQueries({ queryKey: QK.roadmapMap })
    },
    onError: (err) => {
      const code = apiErrorCode(err)
      if (code !== null && REFETCH_ON.has(code)) void qc.invalidateQueries({ queryKey: milestonesKey(slug) })
    },
    meta: PM_MUTATION_META,
  })
}

export const useTickCondition = (slug: string) => useMilestoneWrite(slug, tickCondition)
export const useSetStatusNote = (slug: string) => useMilestoneWrite(slug, setStatusNote)
export const useAddNote = (slug: string) => useMilestoneWrite(slug, addNote)
export const useUpdateNote = (slug: string) => useMilestoneWrite(slug, updateNote)
export const useAdvanceMilestone = (slug: string) => useMilestoneWrite(slug, advanceMilestone)
/** The page owns the one advance mutation, so its error (a 409 `stale_current`
 *  above all) outlives the card whose dialog sent it. */
export type AdvanceMutation = ReturnType<typeof useAdvanceMilestone>
