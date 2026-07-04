import type { PullRequestInfo } from '@/api/types'

/**
 * A PR is *actionable* only while it's still live: `open` or `draft`. A `merged`
 * or `closed` PR is terminal — there's nothing left to fix on it.
 *
 * This is the single gate the attention layer uses to decide whether a PR's
 * checks rollup still matters. Route every "does this PR's checks count?"
 * decision through here so the rule lives in exactly one place.
 */
export function isActionablePrState(state: PullRequestInfo['state']): boolean {
  return state === 'open' || state === 'draft'
}

/**
 * Count of PRs still *in flight* — open or draft — i.e. the pipeline's active
 * workload. Routes through {@link isActionablePrState} so "in flight" and
 * "actionable" can never drift apart. Drives the Pipeline tab's count badge.
 */
export function inFlightPrCount(prs?: PullRequestInfo[] | null): number {
  return prs?.reduce((n, pr) => n + (isActionablePrState(pr.state) ? 1 : 0), 0) ?? 0
}

/**
 * Single source of truth for "this session has a PR whose checks are failing
 * AND that's still worth the user's attention".
 *
 * The data layer (server/github/pr_status.py) keeps `checks` strictly factual:
 * a closed PR *did* have red checks, and GitHub freezes that
 * `statusCheckRollup` at close time forever. The *attention* judgment lives
 * here, not there — a failing rollup only counts when the PR is still
 * actionable (`open`/`draft`). A `merged`/`closed` PR is terminal and must
 * never contribute a failing signal, even with `checks === 'failing'`,
 * because there's no action left to take on it.
 *
 * Drives BOTH the triage bucket (a failing PR lifts a calm session into the
 * "Needs attention" group — see prTier in triageTier.ts) AND the PR dropdown's
 * red tint, so the card's group and the dropdown color can never disagree.
 */
export function anyChecksFailing(prs?: PullRequestInfo[] | null): boolean {
  return !!prs?.some((pr) => pr.checks === 'failing' && isActionablePrState(pr.state))
}
