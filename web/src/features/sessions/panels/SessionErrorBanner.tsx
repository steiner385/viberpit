import { lazy, Suspense } from 'react'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { IconButton } from '@/ui/IconButton'
import type { SessionRuntime } from '@/session/useSessionRuntime'

/** The hold's copy, buttons and confirm load only when a session is held (D-23). */
const SandboxHoldBanner = lazy(() => import('./SandboxHoldBanner'))

/** The attach refusal a held session answers — the same fact the hold states. */
const SANDBOX_ATTACH_REFUSAL = 'SESSION_SANDBOX_FAILED'

/** A session held on its sandbox (D-23): `status: needs_attention` with a sandbox
 *  `pausedReason` — `sandbox_failed`, or `sandbox_escape_suspected` (`escaped`: a file
 *  write landed outside the worktree). `reason` is `session.sandbox.reason`. */
export interface SandboxHold { sessionId: string; reason: string | null; escaped?: boolean }

interface Props {
  rt: Pick<SessionRuntime, 'state' | 'dismissError'>
  /** Held on a failed sandbox: the banner states it and offers Retry / Turn off. */
  sandboxHold?: SandboxHold | null
  className?: string
}

const BOX = 'flex-shrink-0 flex items-start gap-2 px-4 py-3 text-sm bg-danger-wash text-danger border-b border-danger'

/**
 * The runtime's session-level error banner (`rt.state.error`), rendered wherever a
 * session transcript is: the session page (SessionBody) AND the assistant drawer.
 *
 * ONE component, because the banner used to live only in SessionBody. The drawer
 * mounted its own SessionRuntimeProvider but never rendered `state.error`, so an
 * edge error frame (SESSION_NOT_FOUND after a failed wake, 2026-09-14) reached the
 * reducer and was shown nowhere — the user saw a bubble spinning on "waking
 * session…" with no explanation.
 *
 * Transient by design: dismissable here, and retired automatically by the reducer
 * when the next prompt is sent. A turn-anchored error also gets an inline ErrorCard
 * in the transcript, which is the durable record, so closing this never loses it.
 *
 * The one exception is a sandbox hold (D-23), which is durable: it rides the session
 * payload, so it is not dismissable, and it stands in for the attach refusal
 * (`SESSION_SANDBOX_FAILED`) that states the same fact. Its two actions — Retry, and
 * the confirmed "Turn off sandbox and continue" — are an explicit human decision.
 */
export function SessionErrorBanner({ rt, sandboxHold = null, className }: Props) {
  const error = sandboxHold && rt.state.error?.code === SANDBOX_ATTACH_REFUSAL ? null : rt.state.error
  return (
    <>
      {sandboxHold && (
        <Suspense fallback={null}>
          <SandboxHoldBanner {...sandboxHold} className={cn(BOX, 'flex-wrap', className)} />
        </Suspense>
      )}
      {error && <ErrorRow error={error} onDismiss={rt.dismissError} className={className} />}
    </>
  )
}

function ErrorRow({ error, onDismiss, className }: {
  error: NonNullable<Props['rt']['state']['error']>; onDismiss: () => void; className?: string
}) {
  return (
    <div
      role="alert"
      className={cn(BOX, className)}
    >
      <div className="flex-1 min-w-0">
        <span className="font-medium">Error: </span>
        {error.message}
        {error.resetsAt && (
          <span className="ml-2 text-xs opacity-75">
            (Resets at {new Date(error.resetsAt).toLocaleTimeString()})
          </span>
        )}
      </div>
      <IconButton
        aria-label="Dismiss"
        size="sm"
        onClick={onDismiss}
        className="-my-1 shrink-0 text-danger"
      >
        <X size={14} aria-hidden="true" />
      </IconButton>
    </div>
  )
}
