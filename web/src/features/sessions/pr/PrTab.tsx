/**
 * The session's PR tab (D-15): the PRs this session opened — checks, review
 * threads, and the merge actions that apply — without leaving for GitHub.
 *
 * One data path: the PR list is the session payload's `prs` (the per-branch
 * poller's cache); the selected PR's detail is `GET /api/prs/{owner}/{name}/{n}`
 * (`usePrDetail`). When the detail cannot be had, the tab still shows what the
 * payload knows, and says why the live half is missing.
 *
 * Review threads are untrusted text: bodies render only through the transcript's
 * sanitized markdown renderer, and a comment's link only when its url is http(s).
 *
 * Lazy (`SessionBody`), like the Changes and Files tabs.
 */
import { useState, type ReactNode } from 'react'
import { ExternalLink } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Badge, type Tone } from '@/ui/Badge'
import { Button } from '@/ui/Button'
import { Select } from '@/ui/Select'
import { SectionHeading } from '@/ui/SectionHeading'
import { useConfirm } from '@/ui/useConfirm'
import { useFixPr, useMergePr, usePrDetail, useSessionFiles, useSetAutoMerge } from '@/api/hooks'
import { apiErrorCode } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'
import type { PrArmResult, PrDetail, PrDetailCheck, PrMutationResult, PrReviewThread, PullRequestInfo } from '@/api/types'
import { anyChecksFailing, isActionablePrState } from '@/session/prStatus'
import { readyMergeBlock } from '@/pipeline/readyMerge'
import { isSafeHttpUrl } from '@/lib/safeUrl'
import { prUrl } from '@/lib/githubUrl'
import { useComposerDraft } from '@/shell/sessionGestures'
import { Markdown } from '@/features/sessions/transcript/Markdown'
import {
  CHECK_GROUP_ORDER, addressPrompt, defaultPr, groupChecks, hasHumanHold, mergeStateText, orderThreads,
  payloadChecks, prKey, prRepo, threadLocation, type CheckGroupName,
} from './prTabModel'

export interface PrTabProps {
  sessionId: string
  /** The session payload's PRs (`Session.prs`); the tab is listed only when non-empty. */
  prs: PullRequestInfo[]
  /** The session has a worktree, so a thread's path can open in the Files tab. */
  hasFiles: boolean
  /** Open `path` (at `line`) in the Files tab — `useFileTarget`'s setter. */
  onOpenFile: (path: string, line?: number | null) => void
  /** Switch to the Conversation tab (after prefilling the composer). */
  onShowConversation: () => void
}

const HOLD_COPY = 'Held for a human — merge it on GitHub'
const HOLD_UNKNOWN_COPY = 'Couldn\'t confirm the hold state — try again'

const STATE_BADGE: Record<PullRequestInfo['state'], { label: string; tone: Tone }> = {
  open: { label: 'Open', tone: 'success' },
  draft: { label: 'Draft', tone: 'neutral' },
  merged: { label: 'Merged', tone: 'accent' },
  closed: { label: 'Closed', tone: 'danger' },
}

const GROUP_LABEL: Record<CheckGroupName, string> = { failing: 'Failing', pending: 'Pending', passing: 'Passing' }

export default function PrTab({ sessionId, prs, hasFiles, onOpenFile, onShowConversation }: PrTabProps) {
  const [picked, setPicked] = useState<string | null>(null)
  const current = prs.find((p) => prKey(p) === picked) ?? defaultPr(prs)
  if (!current) return null
  return (
    <div className="h-full min-h-0 overflow-y-auto">
      <div className="mx-auto flex w-full max-w-3xl min-w-0 flex-col gap-4 p-3 sm:p-4">
        {prs.length > 1 && (
          <Select aria-label="Pull request" size="sm" className="w-full sm:w-auto sm:max-w-md"
                  value={prKey(current)} onChange={(e) => setPicked(e.target.value)}>
            {prs.map((p) => <option key={prKey(p)} value={prKey(p)}>#{p.number} {p.title}</option>)}
          </Select>
        )}
        <PrPanel key={prKey(current)} sessionId={sessionId} pr={current} hasFiles={hasFiles}
                 onOpenFile={onOpenFile} onShowConversation={onShowConversation} />
      </div>
    </div>
  )
}

/** The head-commit check group's rows as the ready+auto-merge gate reads them. */
function gateChecks(pr: Pick<PullRequestInfo, 'check_groups'>) {
  const head = pr.check_groups?.find((g) => g.kind === 'head')
  return (head?.checks ?? []).map((c) => ({ name: c.name, isRequired: c.required, conclusion: c.conclusion ? c.conclusion.toUpperCase() : null }))
}

/** A mutation refusal in words: the two hold codes get the tab's copy, anything else the server's text. */
function refusalText(e: unknown, fallback: string): string {
  const code = apiErrorCode(e)
  if (code === 'PR_HUMAN_HOLD') return HOLD_COPY
  if (code === 'PR_HUMAN_HOLD_UNKNOWN') return HOLD_UNKNOWN_COPY
  return extractMutationErrorMessage(e, fallback)
}

function armedText(r: PrMutationResult | PrArmResult): string {
  if (!('autoMergeArmed' in r)) return 'Auto-merge armed'
  const parts: string[] = []
  if (r.markedReady) parts.push('Marked ready')
  if (r.alreadyArmed) parts.push('Auto-merge already armed')
  else if (r.autoMergeArmed) parts.push('Auto-merge armed')
  return parts.join(' · ') || 'Done'
}

type Outcome = { ok: boolean; text: string }

function PrPanel({ sessionId, pr, hasFiles, onOpenFile, onShowConversation }: Omit<PrTabProps, 'prs'> & { pr: PullRequestInfo }) {
  const repo = prRepo(pr)
  const detailQuery = usePrDetail(repo, pr.number)
  const d: PrDetail | undefined = detailQuery.data
  const merge = useMergePr()
  const autoMerge = useSetAutoMerge()
  const fix = useFixPr()
  const draft = useComposerDraft()
  const { confirm, dialog } = useConfirm()
  const [busy, setBusy] = useState<string | null>(null)
  const [outcome, setOutcome] = useState<Outcome | null>(null)

  // The detail's cached half is the same poller entry as the payload, refreshed every
  // 30 s; prefer it, else the payload's own fields.
  const state: PullRequestInfo['state'] = d?.merged ? 'merged' : (d?.state ?? pr.state)
  const isDraft = d?.isDraft ?? pr.is_draft ?? state === 'draft'
  const autoMergeOn = d?.autoMergeEnabled ?? pr.auto_merge_enabled ?? false
  const inQueue = d?.in_merge_queue ?? pr.in_merge_queue ?? false
  const mss = d?.mergeStateStatus ?? pr.merge_state_status ?? ''
  const hold = d ? d.humanHold : hasHumanHold(pr.labels)
  const title = d?.title || pr.title
  const url = [d?.url, pr.url, repo ? prUrl(repo, pr.number) : null].find(isSafeHttpUrl) ?? null
  const unavailable = !repo
    ? 'no repository on this PR'
    : detailQuery.isError ? extractMutationErrorMessage(detailQuery.error, 'request failed') : null

  const checks: PrDetailCheck[] = d && d.checks.length ? d.checks : payloadChecks(pr)
  const threads = d?.threads ?? []

  const terminal = !isActionablePrState(state)
  const armBlock = readyMergeBlock({ mergeStateStatus: mss, checks: gateChecks(d ?? pr) })
  const showArm = !terminal && !hold && (isDraft || (state === 'open' && !autoMergeOn))
  const showDisable = !terminal && autoMergeOn
  const showMerge = !terminal && !hold && mss === 'CLEAN' && !isDraft
  const showFix = !terminal && !hold && anyChecksFailing([{ ...pr, state }])
  const showActions = !terminal && (showArm || showDisable || showMerge || showFix || hold)

  async function run(name: string, act: () => Promise<Outcome | null>) {
    setBusy(name)
    setOutcome(null)
    try {
      setOutcome(await act())
    } finally {
      setBusy(null)
    }
  }

  const onArm = () => run('arm', async () => {
    if (!repo) return null
    try {
      const r = await autoMerge.mutateAsync({ repo, number: pr.number, enabled: true })
      return r.ok ? { ok: true, text: armedText(r) } : { ok: false, text: r.error || 'GitHub did not arm auto-merge' }
    } catch (e) { return { ok: false, text: refusalText(e, 'Couldn\'t arm auto-merge') } }
  })

  const onDisable = () => run('disable', async () => {
    if (!repo) return null
    try {
      const r = await autoMerge.mutateAsync({ repo, number: pr.number, enabled: false })
      return r.ok ? { ok: true, text: 'Auto-merge disabled' } : { ok: false, text: r.error || 'GitHub did not disable auto-merge' }
    } catch (e) { return { ok: false, text: refusalText(e, 'Couldn\'t disable auto-merge') } }
  })

  const onMerge = async () => {
    if (!repo) return
    const ok = await confirm({
      title: `Merge #${pr.number} now?`,
      description: `Squash-merges “${title}” into its base on GitHub. This can't be undone from here.`,
      confirmLabel: 'Merge',
      tone: 'warning',
    })
    if (!ok) return
    await run('merge', async () => {
      try {
        const r = await merge.mutateAsync({ repo, number: pr.number })
        return r.ok ? { ok: true, text: 'Merged' } : { ok: false, text: r.error || 'GitHub did not merge it' }
      } catch (e) { return { ok: false, text: refusalText(e, 'Couldn\'t merge') } }
    })
  }

  // A failure toasts globally (useFixPr keeps the default), so only success is said here.
  const onFix = () => run('fix', async () => {
    try {
      await fix.mutateAsync({ id: sessionId, number: pr.number })
      return { ok: true, text: 'Asked the session to fix the failing checks' }
    } catch { return null }
  })

  const onAddress = async (t: PrReviewThread) => {
    if (draft.has(sessionId)) {
      const ok = await confirm({
        title: 'Replace your unsent message?',
        description: 'The message box has text you haven\'t sent. This review comment will replace it.',
        confirmLabel: 'Replace',
        tone: 'warning',
      })
      if (!ok) return
    }
    draft.set(sessionId, addressPrompt(t))
    onShowConversation()
  }

  const badge = STATE_BADGE[isDraft && state === 'open' ? 'draft' : state]
  const mergeText = terminal ? null : mergeStateText(mss)
  const grouped = groupChecks(checks)
  const { unresolved, resolved } = orderThreads(threads)

  return (
    <>
      <section aria-label={`Pull request #${pr.number}`} className="flex min-w-0 flex-col gap-2">
        <h2 className="break-words text-base font-semibold text-ink">{title}</h2>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 text-sm">
          <span className="font-medium tabular-nums text-ink-muted">#{pr.number}</span>
          <Badge tone={badge.tone}>{badge.label}</Badge>
          {!terminal && autoMergeOn && <Badge tone="info">Auto-merge on</Badge>}
          {!terminal && inQueue && <Badge tone="info">In merge queue</Badge>}
          {hold && <Badge tone="warning">Held for a human</Badge>}
          {mergeText && <span className="text-xs text-ink-muted">{mergeText}</span>}
          {url && (
            <a href={url} target="_blank" rel="noopener noreferrer"
               className="ml-auto inline-flex min-h-target items-center gap-1 text-xs text-accent hover:underline">
              Open on GitHub <ExternalLink size={12} aria-hidden="true" />
            </a>
          )}
        </div>
        {unavailable && (
          <p role="status" className="rounded-md bg-warning-wash px-2.5 py-1.5 text-xs text-warning">
            Live details unavailable — {unavailable}
          </p>
        )}
      </section>

      {showActions && (
        <div role="group" aria-label="Pull request actions" className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2">
            {showArm && (
              <Button size="sm" variant="primary" disabled={!!busy || armBlock !== null} loading={busy === 'arm'}
                      title={armBlock ?? 'Mark ready for review and arm auto-merge (squash)'} onClick={onArm}>
                Ready + auto-merge
              </Button>
            )}
            {showDisable && (
              <Button size="sm" variant="secondary" disabled={!!busy} loading={busy === 'disable'} onClick={onDisable}>
                Disable auto-merge
              </Button>
            )}
            {showMerge && (
              <Button size="sm" variant="secondary" disabled={!!busy} loading={busy === 'merge'} onClick={() => void onMerge()}>
                Merge now
              </Button>
            )}
            {showFix && (
              <Button size="sm" variant="secondary" className="border-warning text-warning" disabled={!!busy}
                      loading={busy === 'fix'} onClick={onFix}>
                Fix
              </Button>
            )}
          </div>
          {showArm && armBlock && <p className="text-xs text-ink-muted">{armBlock}</p>}
          {hold && <p className="text-xs text-warning">{HOLD_COPY}</p>}
          {outcome && (
            <p role="status" className={cn('text-xs', outcome.ok ? 'text-success' : 'text-danger')}>
              <span aria-hidden="true">{outcome.ok ? '✓ ' : '✗ '}</span>
              <span className="break-words">{outcome.text}</span>
            </p>
          )}
        </div>
      )}

      <section aria-labelledby={`pr-checks-${pr.number}`} className="flex min-w-0 flex-col gap-1.5">
        <SectionHeading id={`pr-checks-${pr.number}`}>Checks</SectionHeading>
        {checks.length === 0
          ? <p className="text-sm text-ink-muted">No checks reported.</p>
          : CHECK_GROUP_ORDER.filter((g) => grouped[g].length > 0).map((g) => (
              <Collapsible key={g} open={g === 'failing'} summary={`${GROUP_LABEL[g]} (${grouped[g].length})`}
                           tone={g === 'failing' ? 'text-danger' : undefined}>
                <ul className="divide-y divide-edge">
                  {grouped[g].map((c, i) => <CheckLine key={`${c.name}-${i}`} check={c} />)}
                </ul>
              </Collapsible>
            ))}
      </section>

      {threads.length > 0 && (
        <section aria-labelledby={`pr-threads-${pr.number}`} className="flex min-w-0 flex-col gap-2">
          <SectionHeading id={`pr-threads-${pr.number}`}>Review threads</SectionHeading>
          {unresolved.map((t) => (
            <Thread key={t.id} thread={t} sessionId={sessionId} hasFiles={hasFiles} onOpenFile={onOpenFile}
                    onAddress={() => void onAddress(t)} />
          ))}
          {resolved.length > 0 && (
            <Collapsible open={false} summary={`${resolved.length} resolved`}>
              <div className="flex flex-col gap-2 pt-1">
                {resolved.map((t) => (
                  <Thread key={t.id} thread={t} sessionId={sessionId} hasFiles={hasFiles} onOpenFile={onOpenFile} />
                ))}
              </div>
            </Collapsible>
          )}
        </section>
      )}
      {dialog}
    </>
  )
}

function Collapsible({ open, summary, tone, children }: { open: boolean; summary: string; tone?: string; children: ReactNode }) {
  return (
    <details open={open} className="group rounded-md border border-edge bg-surface">
      <summary className={cn('flex min-h-target cursor-pointer items-center px-3 text-sm font-medium text-ink', tone)}>
        {summary}
      </summary>
      <div className="border-t border-edge px-3 py-1">{children}</div>
    </details>
  )
}

function CheckLine({ check }: { check: PrDetailCheck }) {
  const name = check.name || '(unnamed check)'
  const conclusion = check.conclusion ?? (check.status ? check.status.toLowerCase().replace(/_/g, ' ') : 'pending')
  return (
    <li className="flex min-w-0 items-center gap-2 py-1.5 text-sm">
      <span className="min-w-0 flex-1 break-words text-ink">{name}</span>
      <span className="shrink-0 text-xs text-ink-muted">{conclusion}</span>
      {isSafeHttpUrl(check.url) && (
        <a href={check.url} target="_blank" rel="noopener noreferrer" aria-label={`${name} details`}
           className="inline-flex min-h-target shrink-0 items-center gap-1 text-xs text-accent hover:underline">
          Details <ExternalLink size={12} aria-hidden="true" />
        </a>
      )}
    </li>
  )
}

/** `path:line` — a Files-tab link when the path is in the session's worktree (asked
 *  of the worktree's own file search, exact match), plain text otherwise. */
function ThreadPath({ sessionId, thread, hasFiles, onOpenFile }: {
  sessionId: string; thread: PrReviewThread; hasFiles: boolean; onOpenFile: PrTabProps['onOpenFile']
}) {
  const files = useSessionFiles(sessionId, thread.path, { enabled: hasFiles })
  const exists = hasFiles && !!files.data?.files.some((f) => f.path === thread.path)
  const label = threadLocation(thread)
  return exists ? (
    <button type="button" onClick={() => onOpenFile(thread.path, thread.line)}
            className="min-w-0 break-all text-left font-mono text-xs text-accent hover:underline">
      {label}
    </button>
  ) : <span className="min-w-0 break-all font-mono text-xs text-ink">{label}</span>
}

function Thread({ thread, sessionId, hasFiles, onOpenFile, onAddress }: {
  thread: PrReviewThread; sessionId: string; hasFiles: boolean
  onOpenFile: PrTabProps['onOpenFile']; onAddress?: () => void
}) {
  return (
    <article aria-label={threadLocation(thread)} className="flex min-w-0 flex-col gap-2 rounded-md border border-edge bg-surface p-3">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <ThreadPath sessionId={sessionId} thread={thread} hasFiles={hasFiles} onOpenFile={onOpenFile} />
        {thread.outdated && <Badge tone="neutral">Outdated</Badge>}
      </div>
      {thread.comments.map((c, i) => (
        <div key={i} className="min-w-0 border-l-2 border-edge pl-2.5">
          <div className="flex flex-wrap items-center gap-x-2 text-xs text-ink-muted">
            <span className="font-medium text-ink">{c.author ?? 'unknown'}</span>
            {isSafeHttpUrl(c.url) && (
              <a href={c.url} target="_blank" rel="noopener noreferrer" aria-label="View comment on GitHub"
                 className="inline-flex items-center gap-1 text-accent hover:underline">
                View on GitHub <ExternalLink size={12} aria-hidden="true" />
              </a>
            )}
          </div>
          <div className="min-w-0 break-words text-sm text-ink">
            <Markdown>{c.body}</Markdown>
          </div>
        </div>
      ))}
      {onAddress && (
        <div>
          <Button size="sm" variant="secondary" onClick={onAddress}>Ask session to address</Button>
        </div>
      )}
    </article>
  )
}
