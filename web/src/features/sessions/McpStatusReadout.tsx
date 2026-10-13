/**
 * McpStatusReadout — the `MCP n/m` readout in the SessionMeta strip. Live
 * `mcpStatus` events win; the GET snapshot fills in until the first event.
 * Engines without MCP status report no servers, so the readout renders nothing.
 * The popover body is `McpServerList`, shared with the `/mcp` command panel.
 */
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { useSessionMcp } from '@/api/hooks'
import { popOverlay, pushOverlay } from '@/shell/useOverlayState'
import type { McpServerState } from '@/api/types'
import { settingsSectionPath } from '@/shell/navDefs'
import { worstStatus } from '@/features/settings/sections/mcpStatus'
import { McpServerList } from './McpServerList'

interface Props {
  sessionId: string
  account: string
  live: McpServerState[] | null
}

const NO_SERVERS: McpServerState[] = []

export function McpStatusReadout({ sessionId, account, live }: Props) {
  const snapshot = useSessionMcp(sessionId, live == null)
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLSpanElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)

  // Outside-click closes (mirrors SessionActions / CardMenu); Escape is the
  // container's own onKeyDown below.
  useEffect(() => {
    if (!open) return
    function onDocClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDocClick)
    return () => document.removeEventListener('mousedown', onDocClick)
  }, [open])

  // An open popover is an overlay: the shell's shortcuts must not fire through it.
  useEffect(() => {
    if (!open) return
    pushOverlay()
    return () => popOverlay()
  }, [open])

  const servers = live ?? snapshot.data?.servers ?? NO_SERVERS
  if (servers.length === 0) return null

  const connected = servers.filter((s) => s.status === 'connected').length
  const enabledTotal = servers.filter((s) => s.status !== 'disabled').length
  const worst = worstStatus(servers.map((s) => s.status))
  const warn = worst === 'failed' || worst === 'needs-auth'

  return (
    // Not `relative`: the popover anchors to the SessionMeta strip (right-2), like
    // the sub-agent and background panels, so it cannot run off a phone's right edge.
    <span
      ref={rootRef}
      className="inline-flex"
      onKeyDown={(e) => {
        if (open && e.key === 'Escape') {
          e.stopPropagation()
          setOpen(false)
          triggerRef.current?.focus()
        }
      }}
    >
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className={cn(
          'inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md tabular-nums',
          'hover:bg-edge active:bg-edge-strong',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
          warn ? 'text-warning' : 'text-ink-muted',
        )}
      >
        MCP {connected}/{enabledTotal}
      </button>
      {open && (
        <div
          className={cn(
            'absolute right-2 top-full z-sticky mt-1 w-72 max-w-[calc(100vw-1rem)] max-h-72 overflow-y-auto',
            'rounded-md border border-edge bg-surface shadow-lg px-2 py-1',
          )}
        >
          <McpServerList sessionId={sessionId} account={account} servers={servers} />
          <div className="py-1.5 text-xs">
            <Link to={settingsSectionPath('mcp')} className="text-accent hover:underline">
              Manage →
            </Link>
          </div>
        </div>
      )}
    </span>
  )
}

// The `/mcp` panel shares this chunk (see `DeferredMcpStatusReadout`).
export { McpCommandPanel } from './McpCommandPanel'
