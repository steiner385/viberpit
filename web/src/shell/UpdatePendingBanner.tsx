import { RefreshCw } from 'lucide-react'
import { Banner } from '@/ui/Banner'

interface UpdatePendingBannerProps {
  queuedCount: number
  mode?: 'updating' | 'reconnecting'
}

export function UpdatePendingBanner({ queuedCount, mode = 'updating' }: UpdatePendingBannerProps) {
  const title = mode === 'updating' ? 'Update in progress' : 'Reconnecting…'
  const detail = mode === 'updating'
    ? 'Finishing the current turn, then applying a brief restart.'
    : 'Connection lost — your messages are held and will send once it reconnects.'
  return (
    // `motion-reduce:animate-none`, as its sibling `DaemonRestartBanner` already
    // does: the reduced-motion sweep missed this spinner only because it was not
    // showing at the time (2026-10-01 shell critique, minor).
    <Banner tone="info" title={title} icon={<RefreshCw size={18} className="animate-spin motion-reduce:animate-none" aria-hidden="true" />}>
      {detail}
      {queuedCount > 0 ? (
        <> {' '}{queuedCount} message{queuedCount === 1 ? '' : 's'} held.</>
      ) : null}
    </Banner>
  )
}
