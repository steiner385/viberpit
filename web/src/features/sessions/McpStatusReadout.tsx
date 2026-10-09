/**
 * McpStatusReadout — the `MCP n/m` readout in the SessionMeta strip. Live
 * `mcpStatus` events win; the GET snapshot fills in until the first event.
 * Engines without MCP status report no servers, so the readout renders nothing.
 */
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { cn } from '@/lib/utils'
import { useReconnectSessionMcp, useSessionMcp } from '@/api/hooks'
import { useToggleSessionMcp } from '@/api/mcpScopeHooks'
import { apiErrorCode } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { popOverlay, pushOverlay } from '@/shell/useOverlayState'
import type { McpServerState } from '@/api/types'
import { settingsSectionPath } from '@/shell/navDefs'
import { McpStatusPill } from '@/features/settings/sections/McpStatusPill'
import { McpAuthFlow } from '@/features/settings/sections/McpAuthFlow'
import { shortName, worstStatus } from '@/features/settings/sections/mcpStatus'

interface Props {
  sessionId: string
  account: string
  live: McpServerState[] | null
}

const NO_SERVERS: McpServerState[] = []

export function McpStatusReadout({ sessionId, account, live }: Props) {
  const snapshot = useSessionMcp(sessionId, live == null)
  const reconnect = useReconnectSessionMcp(sessionId)
  const toggle = useToggleSessionMcp(sessionId)
  const [open, setOpen] = useState(false)
  const [reauth, setReauth] = useState<string | null>(null)
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
  // Kill switch: the first toggle's 404 mcp_scope_disabled hides the switches.
  const switchesHidden = toggle.isError && apiErrorCode(toggle.error) === 'mcp_scope_disabled'
  const worst = worstStatus(servers.map((s) => s.status))
  const warn = worst === 'failed' || worst === 'needs-auth'

  const errMessage = reconnect.isError ? extractMutationErrorMessage(reconnect.error, 'Reconnect failed') : null
  const toggleError = toggle.isError && !switchesHidden ? extractMutationErrorMessage(toggle.error, 'Toggle failed') : null

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
          <ul className="divide-y divide-edge">
            {servers.map((s) => {
              const short = shortName(s.name)
              const pendingHere = reconnect.isPending && reconnect.variables?.server === s.name
              return (
                <li key={s.name} className="py-1.5 text-xs">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-ink truncate">{short}</span>
                    <McpStatusPill status={s.status} />
                    {!switchesHidden && (
                      <button
                        type="button"
                        role="switch"
                        aria-checked={s.status !== 'disabled'}
                        aria-label={`${short} enabled`}
                        disabled={toggle.isPending}
                        onClick={() => toggle.mutate({ server: s.name, enabled: s.status === 'disabled' })}
                        className={cn(
                          'relative h-4 w-7 shrink-0 rounded-full transition-colors disabled:opacity-50',
                          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                          s.status !== 'disabled' ? 'bg-accent' : 'bg-edge-strong',
                        )}
                      >
                        <span
                          className={cn(
                            'absolute left-0.5 top-0.5 h-3 w-3 rounded-full bg-surface transition-transform',
                            s.status !== 'disabled' && 'translate-x-3',
                          )}
                        />
                      </button>
                    )}
                    {s.toolCount != null && <span className="ml-auto text-ink-muted">{s.toolCount} tools</span>}
                  </div>
                  {s.error && <p className="mt-0.5 text-danger break-words">{s.error}</p>}
                  <div className="mt-1 flex items-center gap-2">
                    {s.status !== 'connected' && s.status !== 'disabled' && (
                      <button
                        type="button"
                        aria-label={`Reconnect ${short}`}
                        disabled={reconnect.isPending}
                        onClick={() => reconnect.mutate({ server: s.name })}
                        className="rounded border border-edge px-2 py-0.5 hover:bg-edge disabled:opacity-50"
                      >
                        {pendingHere ? 'Reconnecting…' : 'Reconnect'}
                      </button>
                    )}
                    {s.status === 'needs-auth' && (
                      <button
                        type="button"
                        aria-label={`Reauth ${short}`}
                        aria-expanded={reauth === s.name}
                        onClick={() => setReauth((r) => (r === s.name ? null : s.name))}
                        className="rounded border border-warning px-2 py-0.5 text-warning hover:bg-warning-wash"
                      >
                        Reauth
                      </button>
                    )}
                  </div>
                  {reauth === s.name && <McpAuthFlow server={short} accounts={[account]} />}
                </li>
              )
            })}
          </ul>
          {errMessage && (
            <p role="alert" className="py-1 text-xs text-danger">
              {errMessage}
            </p>
          )}
          {toggleError && (
            <p role="alert" className="py-1 text-xs text-danger">
              {toggleError}
            </p>
          )}
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
