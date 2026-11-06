import { AlarmClock } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useWakeSession } from '@/api/hooks'
import { Button } from '@/ui/Button'
import { snoozeUntilLabel } from '@/session/snoozePresets'
import type { SessionSnooze } from '@/api/types'

interface Props {
  sessionId: string
  snooze: SessionSnooze
  className?: string
}

/**
 * "Snoozed until X · reason · [Wake]" over an open session's transcript.
 * Mounted by SessionBody only while the record carries a snooze, so the wake
 * hook runs only then. Sending a prompt also wakes the session (server side);
 * the button is the explicit path.
 */
export function SnoozeBanner({ sessionId, snooze, className }: Props) {
  const { mutate: wake, isPending } = useWakeSession()
  return (
    <div
      role="status"
      className={cn(
        'flex-shrink-0 flex items-center gap-2 px-4 py-2 text-sm',
        'bg-surface text-ink-muted border-b border-edge',
        className,
      )}
    >
      <AlarmClock size={16} aria-hidden="true" className="shrink-0" />
      <div className="flex-1 min-w-0 truncate">
        <span className="font-medium text-ink">Snoozed {snoozeUntilLabel(snooze.until)}</span>
        {snooze.by === 'session' && <span> · self-snoozed</span>}
        {snooze.reason && <span title={snooze.reason}> · {snooze.reason}</span>}
      </div>
      <Button
        variant="ghost"
        size="sm"
        loading={isPending}
        onClick={() => wake(sessionId)}
        className="h-auto shrink-0 min-h-11 [@media(hover:hover)]:min-h-0 px-2 py-1 text-xs text-accent"
      >
        Wake
      </Button>
    </div>
  )
}
