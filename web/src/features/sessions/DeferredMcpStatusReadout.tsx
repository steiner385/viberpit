import { Suspense, lazy, type ComponentProps } from 'react'
import type { McpInlineList as McpInlineListType, McpStatusReadout as McpStatusReadoutType } from './McpStatusReadout'
import type { McpCommandPanel as McpCommandPanelType } from './McpCommandPanel'

/**
 * `McpStatusReadout`, loaded off the critical path (same boundary idiom as
 * `DeferredBrowserTab`). The readout drags in the pill, the reauth flow and
 * their hooks; it is a small strip item that renders nothing until the session
 * reports servers, so a null fallback is exactly what the strip shows first.
 * Import `McpStatusReadout` directly from a render path and the weight returns
 * to the entry chunk — `npm run budget` is what notices.
 *
 * The `/mcp` command panel and the rail's inline list ride the same chunk and
 * import site, so they add no chunk or dependency map to the initial graph.
 */
const loadMcp = () => import('./McpStatusReadout')
const McpStatusReadout = lazy(() => loadMcp().then((m) => ({ default: m.McpStatusReadout })))
const McpCommandPanel = lazy(() => loadMcp().then((m) => ({ default: m.McpCommandPanel })))
const McpInlineList = lazy(() => loadMcp().then((m) => ({ default: m.McpInlineList })))

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

export function DeferredMcpInlineList(props: ComponentProps<typeof McpInlineListType>) {
  return (
    <Suspense fallback={null}>
      <McpInlineList {...props} />
    </Suspense>
  )
}
