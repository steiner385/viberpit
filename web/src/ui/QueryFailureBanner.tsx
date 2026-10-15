import { Banner } from './Banner'
import { Button } from './Button'

export interface QueryFailureBannerProps {
  /** The failure sentence, from the caller's vocab (e.g. `DELIVERY.ciError`). */
  message: string
  /** Rows from an earlier successful read are still on screen (failed refresh). */
  hasData: boolean
  onRetry?: () => void
}

/**
 * A failed read, said as a failure: a polite "nothing here yet" empty state is
 * a false claim when the request merely failed. Distinguishes a failed first
 * load (nothing to show) from a failed refresh (last-good rows retained, so
 * flagged stale). The banner is an `alert`, so screen readers announce it.
 */
export function QueryFailureBanner({ message, hasData, onRetry }: QueryFailureBannerProps) {
  return (
    <Banner
      tone="danger"
      title={hasData ? `${message} Showing the last loaded data, which may be out of date.` : message}
      actions={onRetry && <Button size="sm" variant="secondary" onClick={onRetry}>Retry</Button>}
    />
  )
}
