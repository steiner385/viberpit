import { Suspense, lazy, type ComponentProps } from 'react'
import type { McpInlineList as McpInlineListType } from './McpStatusReadout'
import type { McpCommandPanel as McpCommandPanelType } from './McpCommandPanel'

/**
 * The rail's inline MCP list and the `/mcp` command panel, loaded off the
 * critical path (same boundary idiom as `DeferredBrowserTab`). They drag in the
 * server list, the reauth flow and their hooks; the list renders nothing until
 * the session reports servers, so a null fallback is exactly what shows first.
 * Import `./McpStatusReadout` directly from a render path and the weight
 * returns to the entry chunk — `npm run budget` is what notices.
 */
const loadMcp = () => import('./McpStatusReadout')
const McpCommandPanel = lazy(() => loadMcp().then((m) => ({ default: m.McpCommandPanel })))
const McpInlineList = lazy(() => loadMcp().then((m) => ({ default: m.McpInlineList })))

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
