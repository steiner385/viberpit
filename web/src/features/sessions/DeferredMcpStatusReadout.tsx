import { Suspense, lazy, type ComponentProps } from 'react'
import type { McpStatusReadout as McpStatusReadoutType } from './McpStatusReadout'
import type { McpCommandPanel as McpCommandPanelType } from './McpCommandPanel'

/**
 * `McpStatusReadout`, loaded off the critical path (same boundary idiom as
 * `DeferredBrowserTab`). The readout drags in the pill, the reauth flow and
 * their hooks; it is a small strip item that renders nothing until the session
 * reports servers, so a null fallback is exactly what the strip shows first.
 * Import `McpStatusReadout` directly from a render path and the weight returns
 * to the entry chunk — `npm run budget` is what notices.
 *
 * The `/mcp` command panel rides the same chunk and import site, so it adds
 * no chunk or dependency map to the initial graph.
 */
const loadMcp = () => import('./McpStatusReadout')
const McpStatusReadout = lazy(() => loadMcp().then((m) => ({ default: m.McpStatusReadout })))
const McpCommandPanel = lazy(() => loadMcp().then((m) => ({ default: m.McpCommandPanel })))

export function DeferredMcpStatusReadout(props: ComponentProps<typeof McpStatusReadoutType>) {
  return (
    <Suspense fallback={null}>
      <McpStatusReadout {...props} />
    </Suspense>
  )
}

export function DeferredMcpCommandPanel(props: ComponentProps<typeof McpCommandPanelType>) {
  return (
    <Suspense fallback={null}>
      <McpCommandPanel {...props} />
    </Suspense>
  )
}
