/**
 * The search jump's logic (D-11, lazy): which rendered row a hit lands on, which
 * lead-term run in it is the match, and the paging state machine. Plain functions
 * over plain data — `TranscriptView` owns the virtualizer and runs the effects.
 */
import type { Virtualizer } from '@tanstack/react-virtual'
import type { Turn } from '@/session/reducer'
import type { SearchHit } from '@/api/transcript'
import type { RenderRow } from '../transcriptRows'
import { highlightSegments, maskMarkdownHidden } from './highlightRanges'
import { MARK_CURRENT_ATTR } from './searchMarks'

/** How far back a jump may page older history in: past this the hit is reported
 *  unreachable instead of pulling thousands of turns into the view. */
export const MAX_PAGED_TURNS = 2000

/** A jump to one hit. `rank`: how many earlier hits share its `turnId` (and role);
 *  `dupRank`: how many of those also share its snippet — together they pick which
 *  occurrence in the row is this hit. `terms`: the query it answers — carried, not
 *  read from the view, which may not have re-rendered with a new query's terms yet. */
export interface JumpRequest {
  terms: readonly string[]
  turnId: string
  ordinal: number
  role: 'user' | 'assistant'
  rank: number
  dupRank: number
  snippet?: string
  matchStart?: number
  matchEnd?: number
}

/** The request for `hits[index]`, its ranks counted over the earlier hits. */
export function jumpRequest(hits: readonly SearchHit[], index: number, terms: readonly string[]): JumpRequest {
  const hit = hits[index]
  let rank = 0
  let dupRank = 0
  for (let i = 0; i < index; i++) {
    const h = hits[i]
    if (h.turnId !== hit.turnId || h.role !== hit.role) continue
    rank++
    if (h.snippet === hit.snippet) dupRank++
  }
  return {
    terms, turnId: hit.turnId, ordinal: hit.ordinal, role: hit.role, rank, dupRank,
    snippet: hit.snippet, matchStart: hit.matchStart, matchEnd: hit.matchEnd,
  }
}

/** A turn's prose as the server's snippets see it: its text blocks in order, each
 *  whitespace character one space (`server/sessions/search.py` `_snippet`). */
function proseOf(turn: Turn, transform: (raw: string) => string = (raw) => raw): string {
  const raw = turn.blocks
    .map((b) => (b.kind === 'text' ? b.text : ''))
    .filter(Boolean)
    .join('\n')
  // Transform (mask) BEFORE whitespace is flattened: fences are found by line.
  return transform(raw).replace(/\s/g, ' ')
}

/** Which lead-term run of `turn` the hit's match is, from where its snippet sits in
 *  the turn's prose; null when the snippet is not there. Runs are counted the way
 *  they render: an assistant turn is markdown, whose code is never marked. */
function leadFromSnippet(turn: Turn, req: JumpRequest, terms: readonly string[]): number | null {
  if (req.snippet === undefined || req.matchStart === undefined) return null
  const prose = proseOf(turn)
  const counted = turn.role === 'assistant' ? proseOf(turn, maskMarkdownHidden) : prose
  const at: number[] = []
  for (let p = prose.indexOf(req.snippet); p !== -1; p = prose.indexOf(req.snippet, p + 1)) at.push(p)
  if (at.length === 0) return null
  const match = at[Math.min(req.dupRank, at.length - 1)] + req.matchStart
  let lead = 0
  let pos = 0
  for (const seg of highlightSegments(counted, terms)) {
    const end = pos + seg.text.length
    if (seg.lead) {
      if (match < end) return lead
      lead++
    }
    pos = end
  }
  return null
}

/** The reducer's role-scoped turn-id rule (`turnHoldsEngineTurn`), passed in: an
 *  import would pull the reducer out of the entry chunk into a split of its own. */
export type HoldsEngineTurn = (t: Turn, id: string, role: Turn['role']) => boolean

const turnAt = (rows: readonly RenderRow[], i: number): Turn | null => {
  const r = rows[i]
  return r?.kind === 'turn' ? r.turn : null
}

/**
 * The row `req` lands on and the lead run in it, or null when it is not loaded.
 *
 *  1. By id: the user message's / assistant turn's id, role-scoped
 *     (`holds` — the reducer's own `turnHoldsEngineTurn`).
 *  2. By ordinal — an id the client re-keyed: an assistant hit lands on the turn
 *     holding that ordinal; a user hit on a prompt above it, preferring one that
 *     holds the lead term (a Codex steering prompt), else the nearest.
 *  3. By text — a LIVE turn (no ordinal yet): Claude streams one turn under its
 *     first entry's id, while the hit names the later entry that holds the match.
 *     The live turn whose prose holds the hit's snippet (else just its matched
 *     text) is the one.
 *
 * The lead is read from the snippet's position in the row when it is there, else
 * it is the hit's rank among the hits sharing its turn id.
 */
export function resolveHit(
  rows: readonly RenderRow[],
  req: JumpRequest,
  holds: HoldsEngineTurn,
): { index: number; lead: number } | null {
  const { terms } = req
  const lead = (i: number) => leadFromSnippet(turnAt(rows, i)!, req, terms) ?? req.rank
  const byId = rows.findIndex((r) => r.kind === 'turn' && holds(r.turn, req.turnId, req.role))
  if (byId !== -1) return { index: byId, lead: lead(byId) }

  const byOrdinal = rows.findIndex((r) => r.kind === 'turn' && r.turn.role === 'assistant' && r.turn.ordinal === req.ordinal)
  if (byOrdinal !== -1) {
    if (req.role === 'assistant') return { index: byOrdinal, lead: lead(byOrdinal) }
    const leadTerm = terms[0] ?? ''
    let nearest = -1
    for (let i = byOrdinal - 1; i >= 0; i--) {
      const t = turnAt(rows, i)
      if (!t) continue
      if (t.role === 'assistant' && t.ordinal !== undefined && t.ordinal !== req.ordinal) break
      if (t.role !== 'user') continue
      if (nearest === -1) nearest = i
      if (leadTerm && proseOf(t).toLowerCase().includes(leadTerm)) return { index: i, lead: lead(i) }
    }
    const at = nearest === -1 ? byOrdinal : nearest
    return { index: at, lead: lead(at) }
  }

  if (req.snippet !== undefined) {
    const matched = req.snippet.slice(req.matchStart ?? 0, req.matchEnd ?? req.snippet.length)
    const live = rows
      .map((_, i) => ({ t: turnAt(rows, i), i }))
      .filter(({ t }) => t !== null && t.role === req.role && t.ordinal === undefined)
    const hit = live.find(({ t }) => proseOf(t!).includes(req.snippet!))
      ?? live.find(({ t }) => proseOf(t!).toLowerCase().includes(matched.toLowerCase()))
    if (hit) return { index: hit.i, lead: lead(hit.i) }
  }
  return null
}

/** A jump waiting for its turn to be paged in. */
export interface PendingJump extends JumpRequest {
  /** `earliestTurn` when the jump started — the 2000-turn budget is measured from it. */
  startEarliest: number
  /** The `earliestTurn` the last load was asked from, and whether that load has
   *  been seen in flight — a load that settles with `earliestTurn` unchanged
   *  failed, and the jump gives up rather than asking forever. */
  requestedAt: number | null
  sawLoading: boolean
}

export type JumpStep =
  | { kind: 'wait' }
  | { kind: 'load' }
  | { kind: 'fail'; reason: 'too-far' | 'unloadable' | 'missing' }
  | { kind: 'scroll'; index: number; turnId: string; lead: number }

/**
 * Advance `p`: load one more older page — always contiguously, from
 * `earliestTurn`, never a page cut at the hit's own ordinal (a non-contiguous page
 * breaks the history merge) — or, once the hit's turn is loaded, scroll to it.
 * Mutates `p`'s load bookkeeping.
 */
export function stepJump(
  p: PendingJump,
  s: { earliestTurn: number; loadingOlder: boolean; canLoad: boolean; rows: readonly RenderRow[]; holds: HoldsEngineTurn },
): JumpStep {
  if (p.startEarliest - p.ordinal > MAX_PAGED_TURNS) return { kind: 'fail', reason: 'too-far' }
  if (s.earliestTurn > p.ordinal) {
    if (s.loadingOlder) {
      p.sawLoading = true
      return { kind: 'wait' }
    }
    if (p.requestedAt === s.earliestTurn) {
      // Asked from here already: still waiting for the load to start, or it
      // settled without moving `earliestTurn` (a failed fetch).
      return p.sawLoading ? { kind: 'fail', reason: 'unloadable' } : { kind: 'wait' }
    }
    if (!s.canLoad) return { kind: 'fail', reason: 'unloadable' }
    p.requestedAt = s.earliestTurn
    p.sawLoading = false
    return { kind: 'load' }
  }
  const at = resolveHit(s.rows, p, s.holds)
  if (!at) return { kind: 'fail', reason: 'missing' }
  const row = s.rows[at.index]
  return { kind: 'scroll', index: at.index, turnId: row.kind === 'turn' ? row.turn.id : '', lead: at.lead }
}

/** What a jump that cannot land says. */
const JUMP_FAILED: Record<Extract<JumpStep, { kind: 'fail' }>['reason'], string> = {
  'too-far': 'That match is too far back to load — scroll up to page it in',
  unloadable: 'Couldn’t load the history that match is in',
  missing: 'Couldn’t find that match in the conversation',
}

/** How many frames a jump waits for its current mark to render before giving up on
 *  centring the mark (the row stays centred). */
const MARK_WAIT_FRAMES = 120

/** Everything `driveJump` acts through — the transcript's own seams. */
export interface JumpEnv {
  earliestTurn: number
  loadingOlder: boolean
  rows: readonly RenderRow[]
  holds: HoldsEngineTurn
  /** Ask for one more older page (anchored), or undefined with no pager. */
  loadOlder?: () => void
  notify: (message: string) => void
  /** The hit landed on this rendered turn and lead run. */
  land: (current: { turnId: string; lead: number }) => void
  unpin: () => void
  virtualizer: Virtualizer<HTMLDivElement, Element>
  scroller: HTMLElement | null
  /** The pending mark-centring frame, so the user taking over can cancel it. */
  markWait: { current: number | null }
}

/**
 * Run a search jump one step and act on it: ask for one more older page, report a
 * dead end, or land — centre the hit's row, hold every row above it (the hit is
 * centred, so rows between the viewport's top and it are on screen; a hold on rows
 * above the VIEWPORT only would let them settle and push the hit out of view), then
 * centre the current mark itself once it renders (a turn taller than the viewport
 * would otherwise show only its middle). True when the jump is over.
 */
export function driveJump(p: PendingJump, env: JumpEnv): boolean {
  const step = stepJump(p, {
    earliestTurn: env.earliestTurn, loadingOlder: env.loadingOlder, canLoad: !!env.loadOlder,
    rows: env.rows, holds: env.holds,
  })
  switch (step.kind) {
    case 'wait':
      return false
    case 'load':
      env.loadOlder?.()
      return false
    case 'fail':
      env.notify(JUMP_FAILED[step.reason])
      return true
    case 'scroll': {
      const { virtualizer: v, index } = { virtualizer: env.virtualizer, index: step.index }
      env.land({ turnId: step.turnId, lead: step.lead })
      env.unpin()
      v.shouldAdjustScrollPositionOnItemSizeChange = (item) => item.index < index
      v.scrollToIndex(index, { align: 'center' })
      centreOnMark(index, env)
      return true
    }
  }
}

/**
 * Once the hit's mark has rendered, scroll so the MARK is centred. Through
 * `scrollToOffset`, not a raw scrollTop write: a new offset scroll replaces the
 * virtualizer's index reconcile, which would otherwise re-centre the row the moment
 * a row above it re-measures. The transcript's `scrollToFn` flags it as its own,
 * and the hold keeps it in place while rows above settle.
 */
function centreOnMark(index: number, env: JumpEnv): void {
  let frames = 0
  const tick = () => {
    env.markWait.current = null
    const el = env.scroller
    const mark = el?.querySelector<HTMLElement>(`[${MARK_CURRENT_ATTR}]`)
    const row = mark?.closest<HTMLElement>('[data-index]')
    const start = env.virtualizer.measurementsCache[index]?.start
    if (el && mark && row && Number(row.dataset.index) === index && start !== undefined) {
      const m = mark.getBoundingClientRect()
      const r = row.getBoundingClientRect()
      env.virtualizer.scrollToOffset(Math.max(0, start + (m.top - r.top) + m.height / 2 - el.clientHeight / 2))
      return
    }
    if (++frames < MARK_WAIT_FRAMES) env.markWait.current = requestAnimationFrame(tick)
  }
  env.markWait.current = requestAnimationFrame(tick)
}
