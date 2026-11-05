/**
 * McpServerList — one session's MCP servers, one row each: status, enable
 * switch, tool count, error, Reconnect and Reauth. Shared by the session
 * rail's inline list (`McpInlineList`) and the `/mcp` command panel (spec 2026-10-13).
 * Reauth opens `McpAuthFlow` for the session's own account, so the login runs
 * in that account's config dir. With `toolSwitches` (engines with live flag
 * settings — Claude), a connected server expands to its tool switches (D-16b).
 */
import { useState } from 'react'
import { cn } from '@/lib/utils'
import { useReconnectSessionMcp, useSessionMcp } from '@/api/hooks'
import { useSetSessionMcpTool, useToggleSessionMcp } from '@/api/mcpScopeHooks'
import { apiErrorCode } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'
import type { McpServerState } from '@/api/types'
import { McpStatusPill } from '@/features/settings/sections/McpStatusPill'
import { McpAuthFlow } from '@/features/settings/sections/McpAuthFlow'
import { shortName } from '@/features/settings/sections/mcpStatus'
import { McpToolSwitchList } from '@/features/settings/sections/McpToolSwitchList'

interface Props {
  sessionId: string
  account: string
  servers: McpServerState[]
  /** Offer per-tool switches: the engine's `flagSettings` capability (false for Codex). */
  toolSwitches?: boolean
}

const NOT_ENFORCED = 'Tool settings not applied — restart the session'

/** Reauth is offered on any OAuth server (proactive re-sign-in) and on any server needing sign-in. */
function offersReauth(s: McpServerState): boolean {
  return s.status !== 'disabled' && (s.status === 'needs-auth' || s.auth === 'oauth')
}

export function McpServerList({ sessionId, account, servers, toolSwitches = false }: Props) {
  const reconnect = useReconnectSessionMcp(sessionId)
  const toggle = useToggleSessionMcp(sessionId)
  const [reauth, setReauth] = useState<string | null>(null)
  const [toolsOpen, setToolsOpen] = useState<string | null>(null)

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
          // Tool names are only listed while the server is connected (a disabled one lists 0).
          const offersTools = toolSwitches && s.status === 'connected'
          const open = offersTools && toolsOpen === s.name
          const toolsId = `mcp-tools-${sessionId}-${s.name}`
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
              {s.toolsEnforced === false && (
                <p role="status" className="mt-0.5 text-warning break-words">{NOT_ENFORCED}</p>
              )}
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
                {offersTools && (
                  <button
                    type="button"
                    aria-label={`Tools for ${short}`}
                    aria-expanded={open}
                    aria-controls={toolsId}
                    onClick={() => setToolsOpen((o) => (o === s.name ? null : s.name))}
                    className="rounded border border-edge px-2 py-0.5 hover:bg-edge"
                  >
                    {open ? 'Hide tools' : 'Tools'}
                  </button>
                )}
              </div>
              {reauth === s.name && <McpAuthFlow server={short} accounts={[account]} />}
              {open && (
                <div id={toolsId} className="mt-1.5 border-l-2 border-edge pl-2">
                  <SessionToolSwitches sessionId={sessionId} server={s.name} warned={s.toolsEnforced === false} />
                </div>
              )}
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

/** One server's tool switches in a live session. The live `mcpStatus` event carries no
 *  `tools`, so this reads the session MCP GET (fresh on expand); a toggle's reply replaces it.
 *  `warned`: the row already shows the not-enforced notice. */
function SessionToolSwitches({ sessionId, server, warned }: { sessionId: string; server: string; warned: boolean }) {
  const snapshot = useSessionMcp(sessionId, true, true)
  const setTool = useSetSessionMcpTool(sessionId)
  const state = snapshot.data?.servers.find((x) => x.name === server)
  const short = shortName(server)

  let body
  if (snapshot.isPending) body = <p className="text-ink-muted">Loading tools…</p>
  else if (snapshot.isError && !snapshot.data) {
    body = <p role="alert" className="text-danger break-words">{extractMutationErrorMessage(snapshot.error, "Couldn't load the tools.")}</p>
  } else if (!state?.tools) body = <p className="text-ink-muted">Tool list unavailable</p>
  else {
    body = (
      <McpToolSwitchList
        server={short}
        tools={state.tools}
        busy={setTool.isPending}
        pendingTool={setTool.isPending ? setTool.variables?.tool : null}
        onToggle={(tool, enabled) => setTool.mutate({ server, tool, enabled })}
      />
    )
  }
  return (
    <div className="space-y-1">
      {!warned && state?.toolsEnforced === false && <p role="status" className="text-warning break-words">{NOT_ENFORCED}</p>}
      {body}
      {setTool.isError && (
        <p role="alert" className="text-danger break-words">
          {extractMutationErrorMessage(setTool.error, 'Could not change the tool.')}
        </p>
      )}
    </div>
  )
}
