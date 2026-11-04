import { describe, it, expect } from 'vitest'
import type { PullRequestInfo, PrReviewThread } from '@/api/types'
import {
  addressPrompt, defaultPr, groupChecks, mergeStateText, orderThreads, payloadChecks, prKey, prRepo,
} from './prTabModel'

const pr = (over: Partial<PullRequestInfo>): PullRequestInfo => ({
  number: 1, url: 'https://github.com/acme/widgets/pull/1', state: 'open', checks: 'passing',
  title: 't', deployed: false, ...over,
})

const thread = (over: Partial<PrReviewThread>): PrReviewThread => ({
  id: 'T', path: 'src/a.ts', line: 3, resolved: false, outdated: false,
  comments: [{ author: 'alice', body: 'fix', createdAt: null, url: null }], ...over,
})

describe('prTabModel', () => {
  it('reads the repo from the payload, else from the PR url', () => {
    expect(prRepo(pr({ repo: 'o/n' }))).toBe('o/n')
    expect(prRepo(pr({ url: 'https://github.com/acme/widgets/pull/7' }))).toBe('acme/widgets')
    expect(prRepo(pr({ url: 'not a url' }))).toBeNull()
    expect(prKey(pr({ repo: 'o/n', number: 4 }))).toBe('o/n#4')
  })

  it('defaults to the lowest-numbered open or draft PR, else the newest', () => {
    expect(defaultPr([])).toBeNull()
    const picked = defaultPr([
      pr({ number: 9, state: 'open' }), pr({ number: 3, state: 'merged' }), pr({ number: 5, state: 'draft' }),
    ])
    expect(picked?.number).toBe(5)
    expect(defaultPr([pr({ number: 3, state: 'merged' }), pr({ number: 8, state: 'closed' })])?.number).toBe(8)
  })

  it('puts the merge state in words; DRAFT and unknown values as-is', () => {
    expect(mergeStateText('CLEAN')).toBe('Ready to merge')
    expect(mergeStateText('BLOCKED')).toBe('Blocked by required checks or reviews')
    expect(mergeStateText('BEHIND')).toBe('Behind base')
    expect(mergeStateText('DIRTY')).toBe('Has conflicts')
    expect(mergeStateText('UNSTABLE')).toBe('Non-required checks failing')
    expect(mergeStateText('DRAFT')).toBe('DRAFT')
    expect(mergeStateText('HAS_HOOKS')).toBe('HAS_HOOKS')
    expect(mergeStateText('')).toBeNull()
    expect(mergeStateText(undefined)).toBeNull()
  })

  it('groups checks as failing / pending / passing', () => {
    const g = groupChecks([
      { name: 'a', status: 'COMPLETED', conclusion: 'success', url: null },
      { name: 'b', status: 'COMPLETED', conclusion: 'failure', url: null },
      { name: 'c', status: 'IN_PROGRESS', conclusion: null, url: null },
      { name: 'd', status: 'COMPLETED', conclusion: 'skipped', url: null },
      { name: 'e', status: 'COMPLETED', conclusion: 'cancelled', url: null },
      { name: 'f', status: 'COMPLETED', conclusion: 'pending', url: null },
      { name: 'g', status: 'COMPLETED', conclusion: 'error', url: null },
    ])
    expect(g.failing.map((c) => c.name)).toEqual(['b', 'e', 'g'])
    expect(g.pending.map((c) => c.name)).toEqual(['c', 'f'])
    expect(g.passing.map((c) => c.name)).toEqual(['a', 'd'])
  })

  it('payload checks come from the head check group, else checks_detail', () => {
    const fromGroup = payloadChecks(pr({ check_groups: [
      { kind: 'merge', checks: [], total_seconds: 0, cost_usd: null },
      { kind: 'head', total_seconds: 0, cost_usd: null, checks: [
        { name: 'lint', workflow: null, status: 'COMPLETED', conclusion: 'failure', duration_seconds: 1, required: true, overdue: false },
      ] },
    ] }))
    expect(fromGroup).toEqual([{ name: 'lint', status: 'COMPLETED', conclusion: 'failure', url: null }])
    const fromDetail = payloadChecks(pr({ checks_detail: [
      { name: 'unit', conclusion: '', started_at: null, completed_at: null },
    ] }))
    expect(fromDetail).toEqual([{ name: 'unit', status: null, conclusion: null, url: null }])
  })

  it('orders unresolved threads first, resolved separately, in API order', () => {
    const { unresolved, resolved } = orderThreads([
      thread({ id: '1', resolved: true }), thread({ id: '2' }), thread({ id: '3', resolved: true }), thread({ id: '4' }),
    ])
    expect(unresolved.map((t) => t.id)).toEqual(['2', '4'])
    expect(resolved.map((t) => t.id)).toEqual(['1', '3'])
  })

  it('quotes the first comment in the address prompt', () => {
    expect(addressPrompt(thread({ comments: [
      { author: 'a', body: 'Rename this.\nAnd test it.', createdAt: null, url: null },
      { author: 'b', body: 'agreed', createdAt: null, url: null },
    ] }))).toBe('Address this review comment on src/a.ts:3:\n\n> Rename this.\n> And test it.')
    expect(addressPrompt(thread({ line: null }))).toBe('Address this review comment on src/a.ts:\n\n> fix')
  })
})
