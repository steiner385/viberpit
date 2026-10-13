/**
 * McpServerList — one session's MCP servers, one row each: status, enable
 * switch, tool count, error, Reconnect and Reauth. Shared by the `MCP n/m`
 * readout popover and the `/mcp` command panel (spec 2026-10-13).
 * Reauth opens `McpAuthFlow` for the session's own account, so the login runs
 * in that account's config dir.
 */
import { useState } from 'react'
import { cn } from '@/lib/utils'
import { useReconnectSessionMcp } from '@/api/hooks'
import { useToggleSessionMcp } from '@/api/mcpScopeHooks'
import { apiErrorCode } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'
import type { McpServerState } from '@/api/types'
import { McpStatusPill } from '@/features/settings/sections/McpStatusPill'
import { McpAuthFlow } from '@/features/settings/sections/McpAuthFlow'
import { shortName } from '@/features/settings/sections/mcpStatus'

interface Props {
  sessionId: string
  account: string
  servers: McpServerState[]
}

/** Reauth is offered on any OAuth server (proactive re-sign-in) and on any server needing sign-in. */
function offersReauth(s: McpServerState): boolean {
  return s.status !== 'disabled' && (s.status === 'needs-auth' || s.auth === 'oauth')
}

export function McpServerList({ sessionId, account, servers }: Props) {
  const reconnect = useReconnectSessionMcp(sessionId)
  const toggle = useToggleSessionMcp(sessionId)
  const [reauth, setReauth] = useState<string | null>(null)

  // Kill switch: the first toggle's 404 mcp_scope_disabled hides the switches.
  const switchesHidden = toggle.isError && apiErrorCode(toggle.error) === 'mcp_scope_disabled'
  const errMessage = reconnect.isError ? extractMutationErrorMessage(reconnect.error, 'Reconnect failed') : null
  const toggleError = toggle.isError && !switchesHidden ? extractMutationErrorMessage(toggle.error, 'Toggle failed') : null

  return (
    <>
      <ul className="divide-y divide-edge">
        {servers.map((s) => {
          const short = shortName(s.name)
          const pendingHere = reconnect.isPending && reconnect.variables?.server === s.name
          const needsAuth = s.status === 'needs-auth'
          return (
            <li key={s.name} className="py-1.5 text-xs">
              <div className="flex items-center gap-2">
                <span className="font-medium text-ink truncate">{short}</span>
                <McpStatusPill status={s.status} />
                {!switchesHidden && (s.scope === 'user' || s.scope === 'dynamic') && (
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
                {offersReauth(s) && (
                  <button
                    type="button"
                    aria-label={`${needsAuth ? 'Reauth' : 'Re-authenticate'} ${short}`}
                    aria-expanded={reauth === s.name}
                    onClick={() => setReauth((r) => (r === s.name ? null : s.name))}
                    className={cn(
                      'rounded border px-2 py-0.5',
                      needsAuth
                        ? 'border-warning text-warning hover:bg-warning-wash'
                        : 'border-edge text-ink hover:bg-edge',
                    )}
                  >
                    {needsAuth ? 'Reauth' : 'Re-authenticate'}
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
    </>
  )
}
