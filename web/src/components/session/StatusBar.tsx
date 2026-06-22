import { cn } from '@/lib/utils'
import { statusPresentation } from '@/session/statusTier'
import { WorkingIndicator } from './WorkingIndicator'

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

  // Quiet placeholder: the reducer seeds this sentinel while replaying and no
  // live status frame has arrived yet, so we never flash a misleading "Ready".
  const isPlaceholder = status.state === ''

  const { tier, label, Icon, textClass } = statusPresentation(status.state)

  // Per-tier treatment, all sourced from the single status-tier map:
  //   busy      → animated WorkingIndicator (spinner + phrase + elapsed)
  //   needs-you → loud amber, with the sub-typed label
  //   your-turn → calm/quiet (a static dot, neutral text)
  //   error     → loud red
  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className={cn(
        'flex items-center gap-2 text-xs',
        tier === 'needs-you' || tier === 'error'
          ? cn('font-medium', textClass)
          : 'text-gray-500 dark:text-gray-400',
      )}
    >
      {isPlaceholder ? (
        <span aria-hidden="true" className="opacity-50">…</span>
      ) : tier === 'busy' ? (
        <WorkingIndicator count={status.count} />
      ) : (
        <span className="inline-flex items-center gap-1.5">
          <Icon size={12} aria-hidden="true" />
          <span>{label}</span>
        </span>
      )}
      {connectionNote && (
        <span className="text-amber-600 dark:text-amber-400 font-medium">
          {connectionNote}
        </span>
      )}
    </div>
  )
}
