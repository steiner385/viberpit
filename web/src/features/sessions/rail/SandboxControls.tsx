/**
 * The Environment card's Sandbox row (D-23): the state (Off / Active / Starting… /
 * Failed: <reason>), the toggle, and the one-line note while it is on.
 *
 * - Reads the owning daemon's facts (`useSandboxFacts`) — only mounted where the engine
 *   reports the capability, and renders nothing unless the facts confirm it (the kill
 *   switch, Codex, the assistant, an older daemon).
 * - The toggle is disabled, with the reason beside it, while the session is busy or
 *   the host cannot run the sandbox. Turning it off is always allowed.
 * - Both directions restart the session, so both ask first (`ConfirmDialog`).
 * - A refusal is shown inline under the row, never as a bare toast.
 */
import { useEffect, useId, useRef, useState } from 'react'
import { useSandboxFacts, useSetSandbox } from '@/api/sandboxHooks'
import type { SessionSandbox } from '@/api/types'
import { ConfirmDialog } from '@/ui/ConfirmDialog'
import { Switch } from '@/ui/Switch'
import {
  SANDBOX_CONFIRM, SANDBOX_DID_NOT_START, SANDBOX_NOTE, sandboxDisabledReason, sandboxError, sandboxStateLabel,
} from '../sandboxCopy'

export interface SandboxControlsProps {
  sessionId: string
  busy: boolean
  /** `session.sandbox` off the live payload — fresher than the facts' own copy. */
  state?: SessionSandbox | null
}

export function SandboxControls({ sessionId, busy, state = null }: SandboxControlsProps) {
  const labelId = useId()
  const describeId = useId()
  const { data: facts, refetch } = useSandboxFacts(sessionId)
  const toggle = useSetSandbox()
  // The direction outlives `open`, so the dialog keeps its copy while it closes.
  const [confirm, setConfirm] = useState<{ open: boolean; enabled: boolean }>({ open: false, enabled: true })
  const [message, setMessage] = useState<{ tone: 'error' | 'held'; text: string } | null>(null)

  // A turn ending (or starting) changes `busy` and `canToggle` on the daemon: re-read.
  const prevBusy = useRef(busy)
  useEffect(() => {
    if (prevBusy.current !== busy) void refetch()
    prevBusy.current = busy
  }, [busy, refetch])

  // `facts` may be any JSON a stale route answers; only a confirmed capability shows the row.
  if (facts?.capability !== true) return null

  const view = state ?? { enabled: facts.enabled, state: facts.state, reason: facts.reason }
  const disabledReason = sandboxDisabledReason(facts, busy)
  const failed = view.state === 'failed'

  async function apply(enabled: boolean) {
    setMessage(null)
    try {
      const res = await toggle.mutateAsync({ id: sessionId, enabled })
      if (res.held) setMessage({ tone: 'held', text: SANDBOX_DID_NOT_START })
    } catch (err) {
      setMessage({ tone: 'error', text: sandboxError(err) })
    } finally {
      setConfirm((c) => ({ ...c, open: false }))
    }
  }

  const copy = SANDBOX_CONFIRM[confirm.enabled ? 'on' : 'off']

  return (
    <div className="space-y-1" data-testid="sandbox-controls">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div id={labelId} className="text-ink">Sandbox</div>
          <p
            id={describeId}
            className={failed ? 'break-words text-danger' : 'text-ink-muted'}
            data-testid="sandbox-state"
          >
            {sandboxStateLabel(view)}
            {disabledReason && <span className="block text-ink-muted" data-testid="sandbox-disabled-reason">{disabledReason}</span>}
          </p>
        </div>
        <Switch
          checked={view.enabled}
          onChange={(next) => { setMessage(null); setConfirm({ open: true, enabled: next }) }}
          disabled={disabledReason !== null || toggle.isPending}
          aria-labelledby={labelId}
          aria-describedby={describeId}
        />
      </div>
      {view.enabled && <p className="text-xs text-ink-muted" data-testid="sandbox-note">{SANDBOX_NOTE}</p>}
      {message && (
        <p role={message.tone === 'error' ? 'alert' : 'status'} className="text-xs text-danger" data-testid="sandbox-message">
          {message.text}
        </p>
      )}
      <ConfirmDialog
        open={confirm.open}
        onClose={() => setConfirm((c) => ({ ...c, open: false }))}
        onConfirm={() => apply(confirm.enabled)}
        title={copy.title}
        description={copy.description}
        confirmLabel={copy.confirmLabel}
        tone="warning"
        busy={toggle.isPending}
      />
    </div>
  )
}
