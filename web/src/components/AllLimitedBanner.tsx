import { Ban } from 'lucide-react'
import { Banner } from '@/ui/Banner'
import { formatResetTime } from '@/lib/formatReset'

interface AllLimitedBannerProps {
  earliestReset: string | null  // ISO timestamp
  onDismiss: () => void
}

export function AllLimitedBanner({ earliestReset, onDismiss }: AllLimitedBannerProps) {
  const resetLabel = formatResetTime(earliestReset)
  return (
    <Banner tone="warning" role="alert" title="All accounts at limit" icon={<Ban size={18} aria-hidden="true" />} onDismiss={onDismiss}>
      {resetLabel
        ? <>Capacity returns at <time dateTime={earliestReset ?? undefined}>{resetLabel}</time></>
        : 'No reset time available'}
    </Banner>
  )
}
