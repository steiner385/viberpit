import { RefreshCw } from 'lucide-react'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'

interface UpdatePendingBannerProps {
  queuedCount: number
  mode?: 'updating' | 'reconnecting' | 'retrying'
  /** Escape hatches for a hold that never clears on its own. Shown only while messages
   *  are held, or during a drain (where "Send now" just leaves drain mode). */
  onSendNow?: () => void
  onDiscard?: () => void
}

const COPY = {
  updating: {
    title: 'Update in progress',
    detail: 'Finishing the current turn, then applying a brief restart.',
  },
  reconnecting: {
    title: 'Reconnecting…',
    detail: 'Connection lost — your messages are held and will send once it reconnects.',
  },
  retrying: {
    title: 'Message not delivered yet',
    detail: "The session wasn't ready to take it — retrying shortly.",
  },
} as const

export function UpdatePendingBanner({ queuedCount, mode = 'updating', onSendNow, onDiscard }: UpdatePendingBannerProps) {
  const { title, detail } = COPY[mode]
  const showActions = queuedCount > 0 || mode === 'updating'
  return (
    // `motion-reduce:animate-none`, as its sibling `DaemonRestartBanner` already
    // does: the reduced-motion sweep missed this spinner only because it was not
    // showing at the time (2026-10-01 shell critique, minor).
    <Banner
      tone="info"
      title={title}
      icon={<RefreshCw size={18} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />}
      actions={showActions && (onSendNow || onDiscard) ? (
        <>
          {onSendNow && <Button size="sm" onClick={onSendNow}>Send now</Button>}
          {onDiscard && queuedCount > 0 && <Button size="sm" variant="ghost" onClick={onDiscard}>Discard</Button>}
        </>
      ) : undefined}
    >
      {detail}
      {queuedCount > 0 ? (
        <> {' '}{queuedCount} message{queuedCount === 1 ? '' : 's'} held.</>
      ) : null}
    </Banner>
  )
}
