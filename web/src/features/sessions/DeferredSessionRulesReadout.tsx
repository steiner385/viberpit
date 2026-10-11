import { Suspense, lazy, type ComponentProps } from 'react'
import type { SessionRulesReadout as SessionRulesReadoutType } from './SessionRulesReadout'

/** `SessionRulesReadout`, loaded off the critical path (same idiom as `DeferredMcpStatusReadout`). */
const SessionRulesReadout = lazy(() =>
  import('./SessionRulesReadout').then((m) => ({ default: m.SessionRulesReadout })),
)

export function DeferredSessionRulesReadout(props: ComponentProps<typeof SessionRulesReadoutType>) {
  return (
    <Suspense fallback={null}>
      <SessionRulesReadout {...props} />
    </Suspense>
  )
}
