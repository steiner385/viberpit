// web/src/features/sessions/remote/RemoteSessionsSection.tsx
//
// The "Remote" group of the Sessions list: Claude Code Remote Control sessions that
// ViberPit does not run (an interactive `claude` with /remote-control, another SDK
// host, another machine). Rendered only when the feature is on AND there is at least
// one such session. Same collapsible-section shape as `SnoozedSection` /
// `QueuedSection`; the "Open in Claude app" link sits OUTSIDE the row's open link
// (a11y rule — no interactive element nested inside another).
//
// Loaded lazily by `SessionListColumn` (with its hooks module), so neither this nor
// its 15 s poll adds to the initial chunk.
import { useState } from 'react'
import { Link, useMatch } from 'react-router-dom'
import { ChevronDown, ChevronRight, ExternalLink, Radio, Tag } from 'lucide-react'
import { cn } from '@/lib/utils'
import { isSafeHttpUrl } from '@/lib/safeUrl'
import { Badge } from '@/ui/Badge'
import { TIER_TONE } from '@/session/statusTier'
import { REMOTE_SESSION_ROUTE, remoteSessionPath } from '@/session/sessionNavState'
import { useRemoteSessions, type RemoteSession } from '@/api/remoteSessionsHooks'
import { SessionStatusDot } from '../SessionStatusDot'
import {
  remoteMachineLabel, remoteMachineUnnamed, remoteOriginLabel, remoteProjectLabel, remoteStatus,
} from './remoteSessionCopy'
import { NameMachineDialog } from './NameMachineDialog'

export function RemoteSessionsSection() {
  const { data } = useRemoteSessions()
  const [collapsed, setCollapsed] = useState(false)
  const openId = useMatch(REMOTE_SESSION_ROUTE)?.params.remoteId ?? null

  if (!data?.enabled || data.sessions.length === 0) return null
  const { sessions } = data
  const needsYou = sessions.filter((s) => s.needsYou).length

  return (
    <div data-testid="remote-sessions-section">
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        aria-expanded={!collapsed}
        aria-controls="remote-sessions-panel"
        className={cn(
          'flex w-full min-h-11 [@media(hover:hover)]:min-h-0 items-center gap-1 px-1 py-1',
          'text-xs font-medium uppercase tracking-wide text-ink-muted',
          'hover:text-ink',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded',
        )}
      >
        {collapsed
          ? <ChevronRight size={14} aria-hidden="true" />
          : <ChevronDown size={14} aria-hidden="true" />}
        <Radio size={14} aria-hidden="true" />
        <span>Remote ({sessions.length})</span>
        {needsYou > 0 && (
          <Badge tone="warning" className="ml-1 normal-case tracking-normal">{needsYou} need you</Badge>
        )}
      </button>

      {!collapsed && (
        // Server order is the order: needs-you first, then connected, then most recent.
        <ul id="remote-sessions-panel" className="space-y-1 py-1">
          {sessions.map((s) => <RemoteSessionRow key={s.id} session={s} open={s.id === openId} />)}
        </ul>
      )}
    </div>
  )
}

function RemoteSessionRow({ session, open }: { session: RemoteSession; open: boolean }) {
  const { tier, label } = remoteStatus(session)
  // claude.ai sends no cwd for these; the repo + branch it reports is the project.
  const project = remoteProjectLabel(session) ?? remoteOriginLabel(session.origin)
  const machine = remoteMachineLabel(session)
  const connected = session.connection === 'connected'
  const [naming, setNaming] = useState(false)
  return (
    <li
      data-testid="remote-session-row"
      className={cn(
        'flex items-center gap-2 rounded-lg border bg-surface px-2 py-2',
        open ? 'border-accent' : 'border-edge',
      )}
    >
      <Link
        to={remoteSessionPath(session.id)}
        aria-current={open ? 'page' : undefined}
        className="flex-1 min-w-0 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded"
      >
        <div className="flex items-center gap-2 min-w-0">
          <SessionStatusDot tier={tier} testId="remote-status-dot" />
          <span className={cn('text-sm text-ink truncate', session.unread ? 'font-semibold' : 'font-medium')}>
            {session.title || session.id}
          </span>
        </div>
        <div className="flex items-center gap-1.5 text-xs text-ink-muted min-w-0">
          <span
            data-testid="remote-connection-dot"
            aria-hidden="true"
            className={cn('h-1.5 w-1.5 rounded-full flex-shrink-0', connected ? 'bg-success' : 'bg-neutral')}
          />
          <span className="sr-only">{connected ? 'Connected' : 'Disconnected'}</span>
          {project && <span data-testid="remote-project" className="truncate min-w-0">{project}</span>}
          {project && <span aria-hidden="true">·</span>}
          <span data-testid="remote-machine" className="truncate shrink-0 max-w-[45%]">{machine}</span>
        </div>
      </Link>
      {session.machineId && remoteMachineUnnamed(session) && (
        // Outside the row link (a11y: no interactive element nested in another).
        <button
          type="button"
          onClick={() => setNaming(true)}
          aria-label={`Name this machine (${machine})`}
          title="Name this machine"
          className={cn(
            'shrink-0 inline-flex min-h-11 [@media(hover:hover)]:min-h-0 items-center px-1 text-ink-muted hover:text-ink',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded',
          )}
        >
          <Tag size={14} aria-hidden="true" />
        </button>
      )}
      {naming && session.machineId && (
        <NameMachineDialog machineId={session.machineId} onClose={() => setNaming(false)} />
      )}
      <Badge tone={TIER_TONE[tier]} className={cn('shrink-0', tier === 'needs-you' && 'font-semibold')}>
        {label}
      </Badge>
      {isSafeHttpUrl(session.url) && (
        <a
          href={session.url}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={`Open ${session.title || 'session'} in Claude app`}
          title="Open in Claude app"
          className={cn(
            'shrink-0 inline-flex min-h-11 [@media(hover:hover)]:min-h-0 items-center px-1 text-accent',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded',
          )}
        >
          <ExternalLink size={14} aria-hidden="true" />
        </a>
      )}
    </li>
  )
}

