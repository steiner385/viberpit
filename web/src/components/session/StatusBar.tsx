import { cn } from '@/lib/utils'

interface Props {
  status: { state: string; count: number }
  connectionState: string
}

function statusText(status: { state: string; count: number }): string {
  switch (status.state) {
    case 'idle':               return 'Ready'
    case 'working':            return 'Working…'
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
        'flex items-center gap-2 px-3 py-1 text-xs',
        'text-gray-500 dark:text-gray-400',
      )}
    >
      <span>{statusText(status)}</span>
      {connectionNote && (
        <span className="ml-auto text-amber-600 dark:text-amber-400 font-medium">
          {connectionNote}
        </span>
      )}
    </div>
  )
}
