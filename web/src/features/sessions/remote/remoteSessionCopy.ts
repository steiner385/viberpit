// The words and tiers a remote (Remote Control) session is shown with — one table for
// the list row and the page banner, so the two cannot describe the same session
// differently.
import type { RemoteSession } from '@/api/remoteSessionsHooks'
import type { StatusTier } from '@/session/statusTier'

const ORIGIN_LABEL: Record<NonNullable<RemoteSession['origin']>, string> = {
  repl: 'Claude Code CLI',
  sdk: 'SDK',
  auto: 'Auto',
}

/** "Claude Code CLI" / "SDK" / "Auto", or null when claude.ai did not say. */
export function remoteOriginLabel(origin: RemoteSession['origin']): string | null {
  return origin ? ORIGIN_LABEL[origin] : null
}

/** The machine a remote session runs on: the ViberPit host or operator label the server
 *  resolved, else the short id claude.ai tags it with (which cannot tell "this machine"
 *  from another, so it names the id rather than guessing). */
export function remoteMachineLabel(s: Pick<RemoteSession, 'machineId' | 'machineName'> | undefined): string {
  if (s?.machineName) return s.machineName
  return s?.machineId ? `machine ${s.machineId.slice(0, 8)}` : 'an unknown machine'
}

/** True when the machine has only an id — no ViberPit host and no operator label. */
export function remoteMachineUnnamed(s: Pick<RemoteSession, 'machineId' | 'machineName'>): boolean {
  return Boolean(s.machineId) && !s.machineName
}

/** `owner/repo · branch` (or just the repo), with `+N` when the session reports more
 *  repos; null when claude.ai reported none. */
export function remoteProjectLabel(s: Pick<RemoteSession, 'project' | 'branch' | 'projects'>): string | null {
  if (!s.project) return null
  const more = (s.projects?.length ?? 0) > 1 ? ` +${(s.projects?.length ?? 0) - 1}` : ''
  return (s.branch ? `${s.project} · ${s.branch}` : s.project) + more
}

/** The status tier (dot colour, badge tone) and words for a remote session. A
 *  disconnected session is unreachable whatever its last status said. */
export function remoteStatus(s: Pick<RemoteSession, 'status' | 'connection' | 'needsYou'>): {
  tier: StatusTier
  label: string
} {
  if (s.needsYou || s.status === 'requires_action') return { tier: 'needs-you', label: 'Needs you' }
  if (s.connection === 'disconnected') return { tier: 'unreachable', label: 'Disconnected' }
  if (s.status === 'running') return { tier: 'busy', label: 'Working' }
  if (s.status === 'pending') return { tier: 'waiting', label: 'Starting' }
  return { tier: 'your-turn', label: 'Idle' }
}
