import { Suspense, lazy, type ComponentProps } from 'react'
import type { McpStatusReadout as McpStatusReadoutType } from './McpStatusReadout'

/**
 * `McpStatusReadout`, loaded off the critical path (same boundary idiom as
 * `DeferredBrowserTab`). The readout drags in the pill, the reauth flow and
 * their hooks; it is a small strip item that renders nothing until the session
 * reports servers, so a null fallback is exactly what the strip shows first.
 * Import `McpStatusReadout` directly from a render path and the weight returns
 * to the entry chunk — `npm run budget` is what notices.
 */
const McpStatusReadout = lazy(() =>
  import('./McpStatusReadout').then((m) => ({ default: m.McpStatusReadout })),
)

export function DeferredMcpStatusReadout(props: ComponentProps<typeof McpStatusReadoutType>) {
  return (
    <Suspense fallback={null}>
      <McpStatusReadout {...props} />
    </Suspense>
  )
}
