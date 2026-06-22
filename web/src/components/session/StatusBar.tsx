import { cn } from '@/lib/utils'
import { WorkingIndicator } from './WorkingIndicator'
import { statusPresentation } from '@/session/statusTier'

interface Props {
  status: { state: string; count: number }
  connectionState: string
}

export function StatusBar({ status, connectionState }: Props) {
  const connected = connectionState === 'connected'
  const connectionNote = connected
    ? null
    : connectionState === 'reconnecting'
      ? 'Reconnecting…'
      : 'Disconnected'

  const { tier, label, Icon, textClass } = statusPresentation(status.state)

  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className={cn(
        'flex items-center gap-2 px-3 py-1 text-xs',
        tier === 'needs-you' || tier === 'error' ? textClass : 'text-gray-500 dark:text-gray-400',
      )}
    >
      {tier === 'busy' ? (
        <WorkingIndicator count={status.count} />
      ) : (
        <span className={cn('inline-flex items-center gap-1', tier !== 'your-turn' && 'font-medium')}>
          <Icon size={12} aria-hidden="true" />
          {label}
        </span>
      )}
      {connectionNote && (
        <span className="ml-auto text-amber-600 dark:text-amber-400 font-medium">
          {connectionNote}
        </span>
      )}
    </div>
  )
}
