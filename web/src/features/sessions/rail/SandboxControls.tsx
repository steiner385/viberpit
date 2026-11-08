/**
 * The Environment card's Sandbox row (D-23): the state (Off / Active / Starting… /
 * Failed: <reason>), the toggle, and the one-line note while it is on.
 *
 * - Reads the owning daemon's facts (`useSandboxFacts`). Shown where the facts confirm
 *   the capability, AND wherever the session's sandbox is on even without it (the kill
 *   switch turned off under a sandboxed session): an enabled sandbox is always visible
 *   and can always be turned off. Hidden otherwise (Codex, the assistant, an older
 *   daemon, a stale route).
 * - The toggle is disabled, with the reason beside it, while the session is busy or the
 *   host cannot run the sandbox; a host reason offers "Re-check host" (a fresh preflight).
 * - Every direction restarts the session, so it asks first — the shared
 *   `SandboxConfirmDialog`, which turns "off" on a held session into "continue without".
 * - A refusal is shown inline under the row, never as a bare toast.
 */
import { useEffect, useId, useRef, useState } from 'react'
import { useRecheckSandboxHost, useSandboxFacts, useSetSandbox } from '@/api/sandboxHooks'
import type { SessionSandbox } from '@/api/types'
import { Switch } from '@/ui/Switch'
import { TOUCH_FLOOR } from '@/ui/list/listControlStyles'
import { SandboxConfirmDialog } from '../SandboxConfirmDialog'
import {
  SANDBOX_DID_NOT_START, SANDBOX_NOTE, SANDBOX_RECHECK_FAILED, sandboxDisabledReason, sandboxError, sandboxStateLabel,
} from '../sandboxCopy'

export interface SandboxControlsProps {
  sessionId: string
  busy: boolean
  /** `session.sandbox` off the live payload — fresher than the facts' own copy. */
  state?: SessionSandbox | null
  /** Held on a failed sandbox (`pausedReason: "sandbox_failed"`), off the live payload. */
  held?: boolean
}

/** The worktree actions' style (`WorktreeControls`): the house 44px floor on a coarse pointer. */
const ACTION = `rounded px-1.5 py-0.5 text-xs font-medium text-accent hover:bg-edge disabled:opacity-50 ${TOUCH_FLOOR} pointer-coarse:px-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent`

export function SandboxControls({ sessionId, busy, state = null, held = false }: SandboxControlsProps) {
  const labelId = useId()
  const describeId = useId()
  const { data: facts, refetch } = useSandboxFacts(sessionId)
  const toggle = useSetSandbox()
  const recheck = useRecheckSandboxHost()
  // The direction outlives `open`, so the dialog keeps its copy while it closes.
  const [confirm, setConfirm] = useState<{ open: boolean; enabled: boolean }>({ open: false, enabled: true })
  const [message, setMessage] = useState<{ tone: 'error' | 'held'; text: string } | null>(null)

  // A turn ending (or starting) changes `busy` and `canToggle` on the daemon: re-read.
  const prevBusy = useRef(busy)
  useEffect(() => {
    if (prevBusy.current !== busy) void refetch()
    prevBusy.current = busy
  }, [busy, refetch])

  // `facts` may be any JSON a stale route answers: only a real answer counts.
  if (facts === undefined || typeof facts !== 'object' || Array.isArray(facts)) return null
  const view = state ?? { enabled: facts.enabled, state: facts.state, reason: facts.reason }
  if (facts.capability !== true && view.enabled !== true) return null

  const disabled = sandboxDisabledReason(facts, view.enabled, busy)
  const failed = view.state === 'failed'
  const isHeld = held || facts.held

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

  function runRecheck() {
    setMessage(null)
    recheck.mutateAsync(sessionId).catch((err: unknown) => {
      setMessage({ tone: 'error', text: sandboxError(err, SANDBOX_RECHECK_FAILED) })
    })
  }

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
            {disabled && <span className="block text-ink-muted" data-testid="sandbox-disabled-reason">{disabled.text}</span>}
          </p>
          {disabled?.host && (
            <button type="button" className={ACTION} disabled={recheck.isPending} onClick={runRecheck}>
              {recheck.isPending ? 'Re-checking…' : 'Re-check host'}
            </button>
          )}
        </div>
        <Switch
          checked={view.enabled}
          onChange={(next) => { setMessage(null); setConfirm({ open: true, enabled: next }) }}
          disabled={disabled !== null || toggle.isPending}
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
      <SandboxConfirmDialog
        open={confirm.open}
        enabled={confirm.enabled}
        held={isHeld}
        busy={toggle.isPending}
        onClose={() => setConfirm((c) => ({ ...c, open: false }))}
        onConfirm={() => apply(confirm.enabled)}
      />
    </div>
  )
}
