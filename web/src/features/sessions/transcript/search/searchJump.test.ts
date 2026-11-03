// @vitest-environment node
import { describe, it, expect } from 'vitest'
import { buildRenderRows } from '../transcriptRows'
import { transcriptReducer, initialTranscript, turnHoldsEngineTurn, type Turn } from '@/session/reducer'
import type { SessionEvent } from '@/session/events'
import type { SearchHit } from '@/api/transcript'
import { jumpRequest, resolveHit, stepJump, type JumpRequest, type PendingJump } from './searchJump'

const t = (id: string, role: 'user' | 'assistant', text = id, extra: Partial<Turn> = {}): Turn =>
  ({ id, role, blocks: [{ kind: 'text', id: `${id}:x`, text }], ...extra })
const req = (over: Partial<JumpRequest> & Pick<JumpRequest, 'turnId' | 'ordinal'>): JumpRequest =>
  ({ role: 'assistant', rank: 0, dupRank: 0, terms: ['x'], ...over })
const hit = (turnId: string, snippet: string, over: Partial<SearchHit> = {}): SearchHit => {
  const at = snippet.toLowerCase().indexOf('widget')
  return { turnId, ordinal: 0, role: 'assistant', snippet, matchStart: at, matchEnd: at + 6, ...over }
}

describe('resolveHit — which row', () => {
  // Rows: begin(0), u1(1), a1#5(2), u2(3), a2#6(4)
  const turns = [t('u1', 'user'), t('a1', 'assistant', 'a1', { ordinal: 5 }), t('u2', 'user'), t('a2', 'assistant', 'a2', { ordinal: 6 })]
  const rows = buildRenderRows(turns, [], { earliestTurn: 0 })

  it('finds the turn by id and role', () => {
    expect(resolveHit(rows, req({ turnId: 'u2', ordinal: 6, role: 'user', terms: ['x'] }), turnHoldsEngineTurn)?.index).toBe(3)
    expect(resolveHit(rows, req({ turnId: 'a1', ordinal: 5, terms: ['x'] }), turnHoldsEngineTurn)?.index).toBe(2)
  })

  it('finds a Codex turn whose id was role-suffixed or kept as its engine id', () => {
    const codex = [
      t('c1#user', 'user'), t('c1', 'assistant', 'c1', { ordinal: 0 }),
      t('x', 'assistant', 'x', { ordinal: 1, engineTurnId: 'c2' }),
    ]
    const r = buildRenderRows(codex, [])
    expect(resolveHit(r, req({ turnId: 'c1', ordinal: 0, role: 'user', terms: ['x'] }), turnHoldsEngineTurn)?.index).toBe(0)
    expect(resolveHit(r, req({ turnId: 'c1', ordinal: 0, terms: ['x'] }), turnHoldsEngineTurn)?.index).toBe(1)
    expect(resolveHit(r, req({ turnId: 'c2', ordinal: 1, terms: ['x'] }), turnHoldsEngineTurn)?.index).toBe(2)
  })

  it('falls back to the ordinal when the id is not rendered', () => {
    expect(resolveHit(rows, req({ turnId: 'gone', ordinal: 6, terms: ['x'] }), turnHoldsEngineTurn)?.index).toBe(4)
    expect(resolveHit(rows, req({ turnId: 'gone', ordinal: 6, role: 'user', terms: ['x'] }), turnHoldsEngineTurn)?.index).toBe(3)
  })

  it('a re-keyed user hit prefers the prompt above its turn that holds the lead term', () => {
    // A Codex steering prompt re-keyed by its source uuid: two prompts open turn 3.
    const steered = [t('p0', 'user', 'start the widget'), t('p1', 'user', 'and also this'), t('a3', 'assistant', 'ok', { ordinal: 3 })]
    const r = buildRenderRows(steered, [])
    expect(resolveHit(r, req({ turnId: 'gone', ordinal: 3, role: 'user', terms: ['widget'] }), turnHoldsEngineTurn)?.index).toBe(0)
    expect(resolveHit(r, req({ turnId: 'gone', ordinal: 3, role: 'user', terms: ['nothing'] }), turnHoldsEngineTurn)?.index).toBe(1)
  })

  it('is null when neither the id, the ordinal nor the text is loaded', () => {
    expect(resolveHit(rows, req({ turnId: 'gone', ordinal: 2, snippet: 'zzz', matchStart: 0, matchEnd: 3, terms: ['zzz'] }), turnHoldsEngineTurn)).toBeNull()
  })
})

describe('resolveHit — a live Claude turn (3a)', () => {
  // Live, Claude streams every entry of a turn under the FIRST entry's id and with no
  // ordinal; the server's hit names the later entry (B) that holds the match.
  it('lands on the live turn whose prose holds the snippet', () => {
    const events: SessionEvent[] = [
      { type: 'user', turnId: 'u1', text: 'go' },
      { type: 'turnStart', turnId: 'A' },
      { type: 'textDelta', turnId: 'A', text: 'First I looked around.' },
      { type: 'textDelta', turnId: 'A', text: ' Then I fixed the widget for good.' },
    ]
    let state = initialTranscript
    for (const e of events) state = transcriptReducer(state, e)
    const rows = buildRenderRows(state.turns, [], { earliestTurn: 0 })
    const live = rows.findIndex((r) => r.kind === 'turn' && r.turn.id === 'A')
    expect(live).toBeGreaterThan(-1)
    const r = jumpRequest([hit('B', 'Then I fixed the widget for good.', { ordinal: 7 })], 0, ['widget'])
    expect(resolveHit(rows, r, turnHoldsEngineTurn)).toEqual({ index: live, lead: 0 })
  })
})

describe('resolveHit — which match in the row (3b)', () => {
  // One turn, three events, each a hit: "widget" twice in the first, once in the last.
  const prose = 'A widget and a widget.\nNothing here.\nThe last widget.'
  const rows = buildRenderRows([t('a1', 'assistant', prose, { ordinal: 0 })], [])
  const hits = [
    hit('a1', 'A widget and a widget.'),
    hit('a1', 'The last widget.'),
  ]

  it('the k-th hit of a turn is the lead run its snippet sits on', () => {
    expect(resolveHit(rows, jumpRequest(hits, 0, ['widget']), turnHoldsEngineTurn)?.lead).toBe(0)
    // The second hit's snippet starts after two runs in the first sentence.
    expect(resolveHit(rows, jumpRequest(hits, 1, ['widget']), turnHoldsEngineTurn)?.lead).toBe(2)
  })

  it('identical snippets in one turn take successive occurrences', () => {
    const same = buildRenderRows([t('a1', 'assistant', 'Fix the widget.\nFix the widget.', { ordinal: 0 })], [])
    const twice = [hit('a1', 'Fix the widget.'), hit('a1', 'Fix the widget.')]
    expect(resolveHit(same, jumpRequest(twice, 0, ['widget']), turnHoldsEngineTurn)?.lead).toBe(0)
    expect(resolveHit(same, jumpRequest(twice, 1, ['widget']), turnHoldsEngineTurn)?.lead).toBe(1)
  })

  it('without a snippet in the row, the rank among the turn\'s hits is the lead', () => {
    const r = jumpRequest([hit('a1', 'not in the row widget'), hit('a1', 'neither widget')], 1, ['widget'])
    expect(r.rank).toBe(1)
    expect(resolveHit(rows, r, turnHoldsEngineTurn)?.lead).toBe(1)
  })
})

describe('stepJump', () => {
  const pending = (over: Partial<PendingJump> = {}): PendingJump =>
    ({ ...req({ turnId: 'a3', ordinal: 3, terms: ['a3'] }), startEarliest: 10, requestedAt: null, sawLoading: false, ...over })
  const rows = buildRenderRows([t('a3', 'assistant', 'a3', { ordinal: 3 })], [])
  const state = { earliestTurn: 10, loadingOlder: false, canLoad: true, rows, holds: turnHoldsEngineTurn }

  it('loads, waits for the load, loads again from the new earliest, then scrolls', () => {
    const p = pending()
    expect(stepJump(p, state)).toEqual({ kind: 'load' })
    expect(stepJump(p, state)).toEqual({ kind: 'wait' }) // asked; not started yet
    expect(stepJump(p, { ...state, loadingOlder: true })).toEqual({ kind: 'wait' })
    expect(stepJump(p, { ...state, earliestTurn: 6 })).toEqual({ kind: 'load' })
    expect(stepJump(p, { ...state, earliestTurn: 2 })).toEqual({ kind: 'scroll', index: 0, turnId: 'a3', lead: 0 })
  })

  it('a load that settles without moving earliestTurn fails as unloadable', () => {
    const p = pending()
    stepJump(p, state)
    stepJump(p, { ...state, loadingOlder: true })
    expect(stepJump(p, state)).toEqual({ kind: 'fail', reason: 'unloadable' })
  })

  it('more than 2000 turns back fails up front; exactly 2000 still loads', () => {
    expect(stepJump(pending({ startEarliest: 2004 }), { ...state, earliestTurn: 2004 })).toEqual({ kind: 'fail', reason: 'too-far' })
    expect(stepJump(pending({ startEarliest: 2003 }), { ...state, earliestTurn: 2003 })).toEqual({ kind: 'load' })
  })

  it('a loaded hit that resolves to no row fails as missing', () => {
    expect(stepJump(pending({ turnId: 'gone', ordinal: 9 }), { ...state, earliestTurn: 0 }))
      .toEqual({ kind: 'fail', reason: 'missing' })
  })

  it('no pager fails as unloadable', () => {
    expect(stepJump(pending(), { ...state, canLoad: false })).toEqual({ kind: 'fail', reason: 'unloadable' })
  })
})

describe('resolveHit — counts lead runs the way they render (code is skipped)', () => {
  const prose = 'Run `widget` first.\n\n```\nwidget()\n```\n\nThen fix the widget.'
  const snippet = 'Then fix the widget.'
  const at = snippet.indexOf('widget')
  const h = (role: 'user' | 'assistant'): SearchHit =>
    ({ turnId: 'x1', ordinal: 0, role, snippet, matchStart: at, matchEnd: at + 6 })

  it('an assistant (markdown) turn does not count the term inside inline or fenced code', () => {
    const rows = buildRenderRows([t('x1', 'assistant', prose, { ordinal: 0 })], [])
    expect(resolveHit(rows, jumpRequest([h('assistant')], 0, ['widget']), turnHoldsEngineTurn)?.lead).toBe(0)
  })

  it('a user prompt is plain text — every occurrence counts', () => {
    const rows = buildRenderRows([t('x1', 'user', prose)], [])
    expect(resolveHit(rows, jumpRequest([h('user')], 0, ['widget']), turnHoldsEngineTurn)?.lead).toBe(2)
  })
})

describe('resolveHit — text the renderer hides is not counted (links, comments)', () => {
  const snippet = 'then fix the widget.'
  const at = snippet.indexOf('widget')
  const lead = (prose: string) => {
    const rows = buildRenderRows([t('x1', 'assistant', prose, { ordinal: 0 })], [])
    const h: SearchHit = { turnId: 'x1', ordinal: 0, role: 'assistant', snippet, matchStart: at, matchEnd: at + 6 }
    return resolveHit(rows, jumpRequest([h], 0, ['widget']), turnHoldsEngineTurn)?.lead
  }

  it('a term in an inline link\'s URL', () => {
    expect(lead('See [the guide](https://example.com/widget-guide "widget docs"), then fix the widget.')).toBe(0)
  })

  it('a term in an HTML comment', () => {
    expect(lead('Noted. <!-- widget marker --> Now then fix the widget.')).toBe(0)
  })

  it('a term in a reference definition', () => {
    expect(lead('Read [the guide][g].\n\n[g]: https://example.com/widget\n\nthen fix the widget.')).toBe(0)
  })

  it('the link TEXT is shown, so a term there still counts', () => {
    expect(lead('See [the widget](https://example.com/x), then fix the widget.')).toBe(1)
  })
})
