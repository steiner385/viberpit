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

/** The machine a remote session runs on, as the short id claude.ai tags it with.
 *  claude.ai reports an opaque machine id, never a host name, so this cannot tell
 *  "this machine" from another — it names the id rather than guessing. */
export function remoteMachineLabel(machineId: string | null): string {
  return machineId ? `machine ${machineId.slice(0, 8)}` : 'an unknown machine'
}

/** `<origin> · <machine>` — the row's second line. */
export function remoteWhereLabel(s: Pick<RemoteSession, 'origin' | 'machineId'>): string {
  return [remoteOriginLabel(s.origin), s.machineId ? remoteMachineLabel(s.machineId) : null]
    .filter(Boolean).join(' · ')
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
