import { cn } from '@/lib/utils'
import { WorkingIndicator } from './WorkingIndicator'

interface Props {
  status: { state: string; count: number }
  connectionState: string
}

function statusText(status: { state: string; count: number }): string {
  switch (status.state) {
    case 'idle':               return 'Ready'
    case 'waiting-permission': return 'Waiting for permission'
    case 'queued':             return `${status.count} queued`
    default:                   return status.state
  }
}

export function StatusBar({ status, connectionState }: Props) {
  const connected = connectionState === 'connected'
  const connectionNote = connected
    ? null
    : connectionState === 'reconnecting'
      ? 'Reconnecting…'
      : 'Disconnected'

  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className={cn(
        'flex items-center gap-2 text-xs',
        'text-gray-500 dark:text-gray-400',
      )}
    >
      {status.state === 'working' ? (
        <WorkingIndicator count={status.count} />
      ) : (
        <span>{statusText(status)}</span>
      )}
      {connectionNote && (
        <span className="text-amber-600 dark:text-amber-400 font-medium">
          {connectionNote}
        </span>
      )}
    </div>
  )
}
