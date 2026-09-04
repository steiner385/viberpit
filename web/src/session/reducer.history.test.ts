import { describe, it, expect, vi } from 'vitest'
import { transcriptReducer, initialTranscript, buildTurnsFromEvents, findOrderAnomalies } from './reducer'
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

  const aTurn = (id: string, ordinal?: number) => ({ id, role: 'assistant' as const, blocks: [], ordinal })
  const uTurn = (id: string, ownerPrefix?: boolean) => ({ id, role: 'user' as const, blocks: [], ownerPrefix })

  it('findOrderAnomalies reports adjacent ordinal-bearing turns that run backwards', () => {
    expect(findOrderAnomalies([aTurn('a', 1), aTurn('u'), aTurn('b', 2), aTurn('c', 3)]).violations).toEqual([])
    expect(findOrderAnomalies([aTurn('a', 5), aTurn('b', 2), aTurn('c', 9), aTurn('d', 8)]).violations)
      .toEqual([['a', 'b'], ['c', 'd']])
  })

  it('findOrderAnomalies reports HOLES between non-adjacent pages', () => {
    // Ascending but skipping: the heal race spliced a page that does not adjoin.
    const holed = findOrderAnomalies([aTurn('a1', 1), aTurn('a2', 2), aTurn('a9', 9), aTurn('a10', 10)])
    expect(holed.gaps).toEqual([['a2', 'a9']])
    expect(holed.violations).toEqual([])   // order alone looks fine — that is the point
    // Contiguous ordinals, and non-ordinal turns interleaved, are not gaps.
    expect(findOrderAnomalies([aTurn('a1', 1), uTurn('u'), aTurn('a2', 2)]).gaps).toEqual([])
  })

  it('findOrderAnomalies reports DUPLICATE ordinals (transcript rewritten under a page)', () => {
    const dup = findOrderAnomalies([aTurn('a5', 5), aTurn('a5b', 5), aTurn('a6', 6)])
    expect(dup.dupes).toEqual([['a5', 'a5b']])
    expect(dup.violations).toEqual([])     // strict `<` alone would have passed this silently
    expect(dup.gaps).toEqual([])
  })

  it('findOrderAnomalies reports an owner-prefix user turn PINNED after loaded content', () => {
    // The #1000 production shape: ordinals ascend (809, 829) either way, so only the
    // prefix mark distinguishes the broken merge from the fixed one.
    const broken = [aTurn('a809', 809), uTurn('U_phase', true), aTurn('a829', 829)]
    expect(findOrderAnomalies(broken).pinned).toEqual(['U_phase'])
    expect(findOrderAnomalies(broken).violations).toEqual([])
    const fixed = [uTurn('U_phase', true), aTurn('a809', 809), aTurn('a829', 829)]
    expect(findOrderAnomalies(fixed).pinned).toEqual([])       // head of its window: correct
    // An unmarked user turn between pages is a real position, not an anomaly.
    expect(findOrderAnomalies([aTurn('a809', 809), uTurn('U_real'), aTurn('a829', 829)]).pinned).toEqual([])
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

  it('_prependHistory logs transcript.order.gap when a non-adjacent page is spliced', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    const s = { ...initialTranscript, replaying: false, earliestTurn: 20,
      turns: [{ id: 'a20', role: 'assistant' as const, blocks: [], ordinal: 20 }] }
    // A page that ends at ordinal 10 spliced in front of ordinal 20: turns 11-19 are a
    // hole nothing will re-fetch. Ordinals still ascend, so the inversion check is silent.
    transcriptReducer(s, { type: '_prependHistory', earliestTurn: 10,
      events: [{ type: 'turnStart', turnId: 'a10', turnIndex: 10 }, { type: 'turnEnd', turnId: 'a10' }] } as never)
    expect(spy).toHaveBeenCalledWith('transcript.order.gap count=1 first=a10..a20 earliestTurn=10 phase=prepend')
    expect(spy).not.toHaveBeenCalledWith(expect.stringContaining('transcript.order.violation'))
    spy.mockRestore()
  })

  it('_prependHistory logs transcript.order.prefixPinned when a boundary prompt is stranded', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    // On screen: a page-boundary prefix prompt with its own turn below it.
    const s = { ...initialTranscript, replaying: false, earliestTurn: 829,
      turns: [{ id: 'U_phase', role: 'user' as const, blocks: [], ownerPrefix: true },
              { id: 'a829', role: 'assistant' as const, blocks: [], ordinal: 829 }] }
    // A non-adjacent older page arrives WITHOUT U_phase (its real position is in the
    // hole between them), leaving the boundary copy pinned below real content.
    const next = transcriptReducer(s, { type: '_prependHistory', earliestTurn: 700,
      events: [{ type: 'turnStart', turnId: 'a700', turnIndex: 700 }, { type: 'turnEnd', turnId: 'a700' }] } as never)
    expect(next.turns.map((t) => t.id)).toEqual(['a700', 'U_phase', 'a829'])
    expect(spy).toHaveBeenCalledWith('transcript.order.prefixPinned count=1 first=U_phase earliestTurn=700 phase=prepend')
    spy.mockRestore()
  })

  it('replayComplete runs the same order check (phase=replay)', () => {
    const spy = vi.spyOn(clientLogger, 'warn').mockImplementation(() => {})
    const s = { ...initialTranscript, replaying: true, hadReset: false,
      turns: [{ id: 'a9', role: 'assistant' as const, blocks: [], ordinal: 9 },
              { id: 'a4', role: 'assistant' as const, blocks: [], ordinal: 4 }] }
    transcriptReducer(s, { type: 'replayComplete', earliestTurn: 4, totalTurns: 30 } as SessionEvent)
    expect(spy).toHaveBeenCalledWith('transcript.order.violation count=1 first=a9>a4 earliestTurn=4 phase=replay')
    spy.mockRestore()
  })

  // ── owner-prefix merge rule ───────────────────────────────────────────────────

  it('_prependHistory keeps the on-screen copy when the INCOMING one is the boundary prefix', () => {
    // The case "older page always wins" gets wrong: a heal race splices a page whose
    // head prefix duplicates a prompt already on screen at its REAL position.
    const s = { ...initialTranscript, replaying: false, earliestTurn: 829,
      turns: [{ id: 'U_phase', role: 'user' as const, blocks: [] },
              { id: 'a829', role: 'assistant' as const, blocks: [], ordinal: 829 }] }
    const next = transcriptReducer(s, { type: '_prependHistory', earliestTurn: 700,
      events: [{ type: 'user', turnId: 'U_phase', text: 'Start Phase B', ownerPrefix: true },
               { type: 'turnStart', turnId: 'a700', turnIndex: 700 }, { type: 'turnEnd', turnId: 'a700' }] } as never)
    // The incoming prefix copy is dropped; the real position survives where it was.
    expect(next.turns.map((t) => t.id)).toEqual(['a700', 'U_phase', 'a829'])
  })

  it('_prependHistory still lets an older page place a prompt it holds unmarked', () => {
    // Both the shipped #1000 behaviour and the explicit rule: the on-screen copy is the
    // prefix, the incoming page holds the real position, so the incoming copy places it.
    const s = { ...initialTranscript, replaying: false, earliestTurn: 829,
      turns: [{ id: 'U_phase', role: 'user' as const, blocks: [], ownerPrefix: true },
              { id: 'a829', role: 'assistant' as const, blocks: [], ordinal: 829 }] }
    const next = transcriptReducer(s, { type: '_prependHistory', earliestTurn: 809,
      events: [{ type: 'user', turnId: 'U_phase', text: 'Start Phase B' },
               { type: 'turnStart', turnId: 'a809', turnIndex: 809 }, { type: 'turnEnd', turnId: 'a809' }] } as never)
    expect(next.turns.map((t) => t.id)).toEqual(['U_phase', 'a809', 'a829'])
  })

  it('carries the server ownerPrefix mark onto the rebuilt turn, and an unmarked re-emit clears it', () => {
    let s = { ...initialTranscript, replaying: false }
    s = transcriptReducer(s, { type: 'user', turnId: 'U', text: 'hi', ownerPrefix: true } as SessionEvent)
    expect(s.turns[0].ownerPrefix).toBe(true)
    // The page holding its real position finally loads and re-emits it unmarked.
    s = transcriptReducer(s, { type: 'user', turnId: 'U', text: 'hi' } as SessionEvent)
    expect(s.turns[0].ownerPrefix).toBeUndefined()
  })
})
