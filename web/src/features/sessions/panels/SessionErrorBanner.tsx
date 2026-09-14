import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { IconButton } from '@/ui/IconButton'
import type { SessionRuntime } from '@/session/useSessionRuntime'

interface Props { rt: Pick<SessionRuntime, 'state' | 'dismissError'>; className?: string }

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
 */
export function SessionErrorBanner({ rt, className }: Props) {
  const error = rt.state.error
  if (!error) return null
  return (
    <div
      role="alert"
      className={cn(
        'flex-shrink-0 flex items-start gap-2 px-4 py-3 text-sm',
        'bg-danger-wash text-danger',
        'border-b border-danger',
        className,
      )}
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
        onClick={rt.dismissError}
        className="-my-1 shrink-0 text-danger"
      >
        <X size={14} aria-hidden="true" />
      </IconButton>
    </div>
  )
}
