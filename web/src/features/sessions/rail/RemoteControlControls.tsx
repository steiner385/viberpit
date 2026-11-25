/**
 * The Environment card's Remote Control row: whether this session also shows in the
 * Claude apps (claude.ai web, desktop, mobile), whether that is its own choice or the
 * Settings default, and an "Open in Claude app" link while the bridge has answered one.
 *
 * - Passed by `SessionBody` only where the session reports `remoteControlAvailable`
 *   (Claude, a claude.ai account, not the assistant drawer, kill switch on).
 * - The switch sets the session's override and applies live — no respawn, so no confirm.
 * - A refusal is shown inline under the row, never as a bare toast.
 * - When the session can't open a bridge (`unavailableReason`, e.g. `login_needed`: the
 *   account has no usable claude.ai login) the switch is off and disabled, with the reason.
 * - Turning it on for a session still running on the setup-token respawns it onto the
 *   account's claude.ai login server-side (idle only; mid-turn answers REMOTE_CONTROL_BUSY).
 */
import { useId, useState } from 'react'
import { ExternalLink } from 'lucide-react'
import { useSetRemoteControl } from '@/api/remoteControlHooks'
import { isSafeHttpUrl } from '@/lib/safeUrl'
import { Switch } from '@/ui/Switch'
import {
  REMOTE_CONTROL_ACCESS_NOTE, REMOTE_CONTROL_CONNECTING, REMOTE_CONTROL_CONNECTING_NOTE,
  REMOTE_CONTROL_STANDBY, REMOTE_CONTROL_STANDBY_NOTE,
  remoteControlError, remoteControlSourceLabel, remoteControlUnavailableText,
} from '../remoteControlCopy'

export interface RemoteControlControlsProps {
  sessionId: string
  /** `session.remoteControl` — the effective value. */
  enabled: boolean
  /** `session.remoteControlOverride`; null = following the Settings default. */
  override?: boolean | null
  /** `session.remoteControlUrl` — the Claude-app link, or null. */
  url?: string | null
  /** `session.remoteControlUnavailableReason` — set when the session can't open a bridge. */
  unavailableReason?: string | null
  /** `session.remoteControlState` — `connecting` while effectively on with no bridge yet
   *  (never shown as plain "On"). Absent from an older server. */
  state?: string | null
}

export function RemoteControlControls({
  sessionId, enabled: enabledProp, override = null, url = null, unavailableReason = null,
  state = null,
}: RemoteControlControlsProps) {
  const unavailable = !!unavailableReason
  const enabled = enabledProp && !unavailable
  const connecting = enabled && state === 'connecting'
  const standby = enabled && state === 'standby'
  const labelId = useId()
  const describeId = useId()
  const toggle = useSetRemoteControl()
  const [error, setError] = useState<string | null>(null)

  function apply(next: boolean) {
    setError(null)
    toggle.mutateAsync({ id: sessionId, enabled: next }).catch((err: unknown) => {
      setError(remoteControlError(err))
    })
  }

  return (
    <div className="space-y-1" data-testid="remote-control-controls">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div id={labelId} className="text-ink">Remote Control</div>
          <p id={describeId} className="text-ink-muted" data-testid="remote-control-state">
            {unavailable
              ? 'Unavailable'
              : `${connecting ? REMOTE_CONTROL_CONNECTING : standby ? REMOTE_CONTROL_STANDBY : enabled ? 'On' : 'Off'} · ${remoteControlSourceLabel(override)}`}
          </p>
        </div>
        <Switch
          checked={enabled}
          onChange={apply}
          disabled={toggle.isPending || unavailable}
          aria-labelledby={labelId}
          aria-describedby={describeId}
        />
      </div>
      {unavailableReason && (
        <p className="text-xs text-ink-muted" data-testid="remote-control-unavailable">
          {remoteControlUnavailableText(unavailableReason)}
        </p>
      )}
      {connecting && (
        <p className="text-xs text-ink-muted" data-testid="remote-control-connecting">
          {REMOTE_CONTROL_CONNECTING_NOTE}
        </p>
      )}
      {standby && (
        <p className="text-xs text-ink-muted" data-testid="remote-control-standby">
          {REMOTE_CONTROL_STANDBY_NOTE}
        </p>
      )}
      {enabled && <p className="text-xs text-ink-muted" data-testid="remote-control-note">{REMOTE_CONTROL_ACCESS_NOTE}</p>}
      {enabled && !standby && isSafeHttpUrl(url) && (
        <a
          href={url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex min-h-target items-center gap-1 text-xs text-accent hover:underline"
        >
          Open in Claude app <ExternalLink size={12} aria-hidden="true" />
        </a>
      )}
      {error && <p role="alert" className="text-xs text-danger" data-testid="remote-control-message">{error}</p>}
    </div>
  )
}
