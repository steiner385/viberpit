import { describe, it, expect, vi } from 'vitest'
import { transcriptReducer, initialTranscript, buildTurnsFromEvents, findOrderViolations } from './reducer'
import { clientLogger } from '../obs/logger'
import type { SessionEvent } from './events'

const olderTurn: SessionEvent[] = [
  { type: 'user', turnId: 'u-old', text: 'old question' },
  { type: 'turnStart', turnId: 'a-old' },
  { type: 'textDelta', turnId: 'a-old', text: 'old answer' },
  { type: 'turnEnd', turnId: 'a-old' },
]

describe('history paging reducer', () => {
  it('records extent on replayComplete', () => {
    const s = transcriptReducer(initialTranscript,
      { type: 'replayComplete', earliestTurn: 5, totalTurns: 25 } as SessionEvent)
    expect(s.earliestTurn).toBe(5)
    expect(s.totalTurns).toBe(25)
    expect(s.replaying).toBe(false)
  })

  it('prepends older turns ahead of existing turns and lowers earliestTurn', () => {
    // replaying: false — paging only happens on a settled view (a prepend landing
    // mid-replay is the paging/heal race and is dropped, see the test below).
    let s = { ...initialTranscript, replaying: false, earliestTurn: 5,
      turns: [{ id: 'a-cur', role: 'assistant' as const, blocks: [] }] }
    s = transcriptReducer(s, { type: '_prependHistory', events: olderTurn, earliestTurn: 4 } as never)
    expect(s.turns[0].id).toBe('u-old')   // older turn now first
    expect(s.turns[s.turns.length - 1].id).toBe('a-cur')
    expect(s.earliestTurn).toBe(4)
    expect(s.loadingOlder).toBe(false)
  })

  it('replayReset clears extent', () => {
    let s = { ...initialTranscript, earliestTurn: 5, totalTurns: 25 }
    s = transcriptReducer(s, { type: 'replayReset' } as SessionEvent)
    expect(s.earliestTurn).toBe(0)
    expect(s.totalTurns).toBe(0)
  })

  it('buildTurnsFromEvents reuses the reducer to produce turns', () => {
    const turns = buildTurnsFromEvents(olderTurn)
    expect(turns.map((t) => t.id)).toEqual(['u-old', 'a-old'])
  })

  it('drops an in-flight prepend that lands during a heal replay (paging/heal race)', () => {
    // Scroll-up fires a fetch, a gap-heal's replayReset lands before the response:
    // the page was fetched against the pre-heal window and no longer adjoins what
    // the replay is rebuilding — prepending it would splice a silent hole.
    let s = { ...initialTranscript, replaying: false, earliestTurn: 5, loadingOlder: true,
      turns: [{ id: 'a-cur', role: 'assistant' as const, blocks: [] }] }
    s = transcriptReducer(s, { type: 'replayReset' } as SessionEvent)
    expect(s.replaying).toBe(true)
    s = transcriptReducer(s, { type: '_prependHistory', events: olderTurn, earliestTurn: 4 } as never)
    expect(s.turns).toHaveLength(0)      // page dropped — the replay owns the rebuild
    expect(s.loadingOlder).toBe(false)   // spinner cleared so scroll-up can re-fetch
    expect(s.earliestTurn).toBe(0)       // replayReset's cleared extent is preserved
  })

  it('still prepends normally when no replay is in flight', () => {
    let s = { ...initialTranscript, replaying: false, earliestTurn: 5,
      turns: [{ id: 'a-cur', role: 'assistant' as const, blocks: [] }] }
    s = transcriptReducer(s, { type: '_prependHistory', events: olderTurn, earliestTurn: 4 } as never)
    expect(s.turns[0].id).toBe('u-old')
    expect(s.earliestTurn).toBe(4)
  })

  // ── page-boundary ordering (session 888ad2bf, 2026-09-03) ────────────────────
  // The server prepends a window's OWNING prompt to any page whose head lands
  // mid-logical-turn. The same prompt id therefore arrives once per page until the
  // page holding its real position loads. The older page must win.

  const settled = (turns: SessionEvent[]) => ({
    ...initialTranscript, replaying: false, earliestTurn: 909,
    turns: buildTurnsFromEvents(turns),
  })

  it('moves a boundary-prefixed prompt to its real position when the older page holding it arrives', () => {
    // Attach window (turns 909..): owner prefix U_gh (real position is in page 889..908).
    const attachWindow: SessionEvent[] = [
      { type: 'user', turnId: 'U_gh', text: 'Set up the GH Projects hierarchy', ts: 3 },
      { type: 'turnStart', turnId: 'a909' }, { type: 'textDelta', turnId: 'a909', text: 'x', ts: 5 }, { type: 'turnEnd', turnId: 'a909' },
    ]
    // Page before=909 (turns 889..908): prefix U_ready, then a889, then U_gh at its REAL position, then a908.
    const page909: SessionEvent[] = [
      { type: 'user', turnId: 'U_ready', text: 'are we ready to dogfood this?', ts: 1 },
      { type: 'turnStart', turnId: 'a889' }, { type: 'textDelta', turnId: 'a889', text: 'y', ts: 2 }, { type: 'turnEnd', turnId: 'a889' },
      { type: 'user', turnId: 'U_gh', text: 'Set up the GH Projects hierarchy', ts: 3 },
      { type: 'turnStart', turnId: 'a908' }, { type: 'textDelta', turnId: 'a908', text: 'z', ts: 4 }, { type: 'turnEnd', turnId: 'a908' },
    ]
    let s = settled(attachWindow)
    expect(s.turns.map((t) => t.id)).toEqual(['U_gh', 'a909'])
    s = transcriptReducer(s, { type: '_prependHistory', events: page909, earliestTurn: 889 } as never)
    expect(s.turns.map((t) => t.id)).toEqual(['U_ready', 'a889', 'U_gh', 'a908', 'a909'])
    expect(s.turns.filter((t) => t.id === 'U_gh')).toHaveLength(1)
  })

  it('keeps a repeated owner prefix at the top of the OLDEST loaded page, not at a page boundary', () => {
    // Mega-turn: pages 829..848 and 809..828 are both prefixed with U_phase.
    const page849: SessionEvent[] = [
      { type: 'user', turnId: 'U_phase', text: 'Start Phase B', ts: 1 },
      { type: 'turnStart', turnId: 'a829' }, { type: 'textDelta', turnId: 'a829', text: 'b', ts: 20 }, { type: 'turnEnd', turnId: 'a829' },
    ]
    const page829: SessionEvent[] = [
      { type: 'user', turnId: 'U_phase', text: 'Start Phase B', ts: 1 },
      { type: 'turnStart', turnId: 'a809' }, { type: 'textDelta', turnId: 'a809', text: 'a', ts: 10 }, { type: 'turnEnd', turnId: 'a809' },
    ]
    let s = settled([{ type: 'turnStart', turnId: 'a849' }, { type: 'turnEnd', turnId: 'a849' }])
    s = transcriptReducer(s, { type: '_prependHistory', events: page849, earliestTurn: 829 } as never)
    expect(s.turns.map((t) => t.id)).toEqual(['U_phase', 'a829', 'a849'])
    s = transcriptReducer(s, { type: '_prependHistory', events: page829, earliestTurn: 809 } as never)
    // Before the fix this was ['a809', 'U_phase', 'a829', 'a849'] — the prompt pinned at the boundary.
    expect(s.turns.map((t) => t.id)).toEqual(['U_phase', 'a809', 'a829', 'a849'])
  })

  it('leaves the existing turns untouched when the page has no overlap', () => {
    let s = settled([{ type: 'turnStart', turnId: 'a-cur' }, { type: 'turnEnd', turnId: 'a-cur' }])
    s = transcriptReducer(s, { type: '_prependHistory', events: olderTurn, earliestTurn: 4 } as never)
    expect(s.turns.map((t) => t.id)).toEqual(['u-old', 'a-old', 'a-cur'])
  })

  // ── ordinals + order telemetry ────────────────────────────────────────────────

  it('turnStart records the server turnIndex as the turn ordinal', () => {
    let s = { ...initialTranscript, replaying: false }
    s = transcriptReducer(s, { type: 'turnStart', turnId: 'a7', turnIndex: 7 } as SessionEvent)
    s = transcriptReducer(s, { type: 'turnStart', turnId: 'live' } as SessionEvent)
    expect(s.turns[0].ordinal).toBe(7)
    expect(s.turns[1].ordinal).toBeUndefined()
  })

  it('findOrderViolations reports adjacent ordinal-bearing turns that run backwards', () => {
    const t = (id: string, ordinal?: number) => ({ id, role: 'assistant' as const, blocks: [], ordinal })
    expect(findOrderViolations([t('a', 1), t('u'), t('b', 2), t('c', 3)])).toEqual([])
    expect(findOrderViolations([t('a', 5), t('b', 2), t('c', 9), t('d', 8)])).toEqual([['a', 'b'], ['c', 'd']])
  })

  it('_prependHistory logs transcript.order.violation when the merge breaks ordinal order', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    const s = { ...initialTranscript, replaying: false, earliestTurn: 20,
      turns: [{ id: 'a20', role: 'assistant' as const, blocks: [], ordinal: 20 }] }
    // A page that claims to be older but carries a HIGHER ordinal (server/cursor skew).
    const next = transcriptReducer(s, { type: '_prependHistory', earliestTurn: 10,
      events: [{ type: 'turnStart', turnId: 'a25', turnIndex: 25 }, { type: 'turnEnd', turnId: 'a25' }] } as never)
    expect(next.turns.map((t) => t.id)).toEqual(['a25', 'a20'])
    expect(spy).toHaveBeenCalledWith(expect.stringContaining('transcript.order.violation count=1 first=a25>a20 earliestTurn=10 phase=prepend'))
    spy.mockRestore()
  })

  it('_prependHistory stays silent on an ordered merge', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    const s = { ...initialTranscript, replaying: false, earliestTurn: 20,
      turns: [{ id: 'a20', role: 'assistant' as const, blocks: [], ordinal: 20 }] }
    transcriptReducer(s, { type: '_prependHistory', earliestTurn: 19,
      events: [{ type: 'turnStart', turnId: 'a19', turnIndex: 19 }, { type: 'turnEnd', turnId: 'a19' }] } as never)
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })
})
