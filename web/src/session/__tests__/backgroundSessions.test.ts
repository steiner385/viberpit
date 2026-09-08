// @vitest-environment node
//
// The foreground/background split of the live sessions list.
//
// Two facts decide it, and only two: the session's `workClass` (published by
// the server from the automation registry — the browser holds no list of
// which origins are housekeeping) and its triage tier. A maintenance session
// is background while an agent is on it or it is finished; the moment it
// needs a human it LIFTS into the foreground, and drops back once resolved.
import { describe, it, expect } from 'vitest'
import { mkSession } from '@/test/factories'
import type { Session, SessionStatus } from '@/api/types'
import { QUIET_TIERS, isBackgroundSession, partitionByAttention } from '../backgroundSessions'

const maint = (id: string, over: Partial<Session> = {}): Session =>
  mkSession(id, { workClass: 'maintenance', origin: 'pr-doctor', originLabel: 'PR Doctor', ...over })

describe('isBackgroundSession', () => {
  it('is never true for a human session, whatever its status', () => {
    for (const status of ['in_progress', 'idle', 'done', 'complete', 'waiting_input', 'queued'] as const) {
      expect(isBackgroundSession(mkSession('h', { status }))).toBe(false)
    }
  })

  it('is never true for a development-class automation session (backlog, sdlc)', () => {
    expect(isBackgroundSession(
      mkSession('b', { status: 'in_progress', workClass: 'development', origin: 'backlog-runner' }),
    )).toBe(false)
  })

  it('treats a session with no workClass at all as foreground (fail visible)', () => {
    // An older server, or a payload merged from a mutation response that
    // omitted the field: nothing may be hidden on a guess.
    expect(isBackgroundSession(mkSession('u', { status: 'in_progress', origin: 'pr-doctor' }))).toBe(false)
  })

  // busy: in_progress / condensing · waiting: queued / waiting_external ·
  // complete: complete / ended
  const QUIET: SessionStatus[] = [
    'in_progress', 'condensing', 'queued', 'waiting_external', 'complete', 'ended',
  ]
  it.each(QUIET)('a maintenance session that is %s stays in the background', (status) => {
    expect(isBackgroundSession(maint('m', { status }))).toBe(true)
  })

  // needs-you: waiting_input / waiting_question / ready_review / pending_approval ·
  // error: needs_attention / limited · your-turn: done / idle
  const LOUD: SessionStatus[] = [
    'waiting_input', 'waiting_question', 'ready_review', 'pending_approval',
    'needs_attention', 'limited', 'done', 'idle',
  ]
  it.each(LOUD)('a maintenance session that is %s lifts into the foreground', (status) => {
    expect(isBackgroundSession(maint('m', { status }))).toBe(false)
  })

  it('lifts a finished maintenance session whose PR checks are failing', () => {
    // `sessionTriage` escalates an at-rest session with a red PR to the error
    // tier; the split reads that same triage, so the card and the scope agree.
    const red = maint('m', {
      status: 'complete',
      prs: [{ number: 1, url: 'u', title: 't', state: 'open', checks: 'failing', deployed: false }],
    })
    expect(isBackgroundSession(red)).toBe(false)
  })

  it('lifts a maintenance session on an unreachable host', () => {
    expect(isBackgroundSession(maint('m', { status: 'in_progress', liveness: 'unreachable' }))).toBe(false)
  })

  it('names the quiet tiers as a closed set, so a new tier lifts by default', () => {
    expect([...QUIET_TIERS].sort()).toEqual(['busy', 'complete', 'waiting'])
  })
})

describe('partitionByAttention', () => {
  it('splits in one pass and keeps each side in input order', () => {
    const a = mkSession('a')
    const b = maint('b', { status: 'in_progress' })
    const c = maint('c', { status: 'waiting_input' })
    const d = maint('d', { status: 'queued' })
    const { foreground, background } = partitionByAttention([a, b, c, d])
    expect(foreground.map((s) => s.id)).toEqual(['a', 'c'])
    expect(background.map((s) => s.id)).toEqual(['b', 'd'])
  })
})
