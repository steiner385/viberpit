/**
 * The session PR tab's pure rules (D-15): which PR it opens on, the merge state in
 * words, how checks group, how threads order, and the "address this" prompt.
 */
import type { PrDetailCheck, PrReviewThread, PullRequestInfo } from '@/api/types'
import { isActionablePrState } from '@/session/prStatus'

const PR_URL = /^https:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/\d+/

/** `owner/name` of a session PR: the payload's `repo`, else read off its GitHub url. */
export function prRepo(pr: Pick<PullRequestInfo, 'repo' | 'url'>): string | null {
  return pr.repo || PR_URL.exec(pr.url)?.[1] || null
}

/** A PR's identity across repos: `owner/name#N` (a session can open PRs in two repos). */
export function prKey(pr: Pick<PullRequestInfo, 'repo' | 'url' | 'number'>): string {
  return `${prRepo(pr) ?? ''}#${pr.number}`
}

/** The PR the tab opens on: the lowest-numbered open or draft one, else the newest. */
export function defaultPr(prs: readonly PullRequestInfo[]): PullRequestInfo | null {
  const live = prs.filter((p) => isActionablePrState(p.state))
  const pool = live.length ? live : prs
  if (!pool.length) return null
  return pool.reduce((best, p) => (live.length ? p.number < best.number : p.number > best.number) ? p : best)
}

const MERGE_STATE_TEXT: Record<string, string> = {
  CLEAN: 'Ready to merge',
  BLOCKED: 'Blocked by required checks or reviews',
  BEHIND: 'Behind base',
  DIRTY: 'Has conflicts',
  UNSTABLE: 'Non-required checks failing',
}

/** GitHub's mergeStateStatus in words; DRAFT and anything unmapped as-is; none → null. */
export function mergeStateText(status: string | null | undefined): string | null {
  if (!status) return null
  return MERGE_STATE_TEXT[status] ?? status
}

export type CheckGroupName = 'failing' | 'pending' | 'passing'
export const CHECK_GROUP_ORDER: readonly CheckGroupName[] = ['failing', 'pending', 'passing']

const PASSING = new Set(['SUCCESS', 'NEUTRAL', 'SKIPPED'])
const PENDING = new Set(['', 'PENDING', 'EXPECTED', 'QUEUED', 'IN_PROGRESS', 'WAITING', 'REQUESTED'])

/** Which group a check sits in. Display only: any finished non-success (cancelled,
 *  a status context's `error`) reads as failing here, whereas the ready+auto-merge
 *  gate (`pipeline/readyMerge`) blocks on its own narrower set. */
function checkGroup(c: PrDetailCheck): CheckGroupName {
  const conclusion = (c.conclusion ?? '').toUpperCase()
  if (PENDING.has(conclusion)) return 'pending'
  if (c.status && c.status.toUpperCase() !== 'COMPLETED') return 'pending'
  return PASSING.has(conclusion) ? 'passing' : 'failing'
}

export function groupChecks(checks: readonly PrDetailCheck[]): Record<CheckGroupName, PrDetailCheck[]> {
  const out: Record<CheckGroupName, PrDetailCheck[]> = { failing: [], pending: [], passing: [] }
  for (const c of checks) out[checkGroup(c)].push(c)
  return out
}

/** The session payload's own checks, for when the live detail is unavailable: the
 *  head-commit check group (it has status), else `checks_detail`. No urls in either. */
export function payloadChecks(pr: PullRequestInfo): PrDetailCheck[] {
  const head = pr.check_groups?.find((g) => g.kind === 'head')
  if (head) return head.checks.map((c) => ({ name: c.name, status: c.status, conclusion: c.conclusion || null, url: null }))
  return (pr.checks_detail ?? []).map((c) => ({ name: c.name, status: null, conclusion: c.conclusion || null, url: null }))
}

/** Unresolved threads (shown) and resolved ones (collapsed), each in GitHub's order. */
export function orderThreads(threads: readonly PrReviewThread[]): { unresolved: PrReviewThread[]; resolved: PrReviewThread[] } {
  return { unresolved: threads.filter((t) => !t.resolved), resolved: threads.filter((t) => t.resolved) }
}

/** `path:line`, or the bare path for a thread with no line. */
export function threadLocation(t: Pick<PrReviewThread, 'path' | 'line'>): string {
  return t.line != null ? `${t.path}:${t.line}` : t.path
}

/** The composer text "Ask session to address" prefills: the location and the
 *  thread's first comment, quoted. */
export function addressPrompt(t: PrReviewThread): string {
  const body = t.comments[0]?.body ?? ''
  const quoted = body.split('\n').map((l) => `> ${l}`).join('\n')
  return `Address this review comment on ${threadLocation(t)}:\n\n${quoted}`
}
