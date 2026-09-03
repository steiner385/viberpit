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
    <Banner tone="info" title={title} icon={<RefreshCw size={18} className="animate-spin" aria-hidden="true" />}>
      {detail}
      {queuedCount > 0 ? (
        <> {' '}{queuedCount} message{queuedCount === 1 ? '' : 's'} held.</>
      ) : null}
    </Banner>
  )
}
