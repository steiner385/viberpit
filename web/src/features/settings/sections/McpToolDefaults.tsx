/**
 * Settings › MCP per-tool defaults (D-16b). Settings has no live session, so the list is the
 * server's last-seen one (from a Test or a live session poll), with "Seen <relative time>".
 * Both pieces read the scope query `McpScopeControl` reads, and render nothing while the scope
 * feature is off (404 `mcp_scope_disabled`) or still loading.
 */
import { useMcpScope, useSetMcpToolDefault } from '@/api/mcpScopeHooks'
import { McpToolSwitchList } from './McpToolSwitchList'
import { mcpMutationErrorMessage, relativeAge, shortName } from './mcpStatus'

const EMPTY_HINT = 'Test the server to load its tools.'

/** Sits beside the row's Test button: the Tools disclosure, or — with no list seen yet — the hint. */
export function McpToolsToggle({ serverKey, open, onToggle, controls }: {
  serverKey: string
  open: boolean
  onToggle: () => void
  /** Id of the panel the disclosure opens. */
  controls: string
}) {
  const scope = useMcpScope()
  const entry = scope.data?.servers?.[serverKey]
  if (!entry) return null
  if (!entry.tools) return <span className="text-xs text-ink-muted">{EMPTY_HINT}</span>
  return (
    <button
      type="button"
      aria-label={`Tools for ${shortName(serverKey)}`}
      aria-expanded={open}
      aria-controls={controls}
      onClick={onToggle}
      className="rounded border border-edge px-2 py-1 text-xs hover:bg-surface-raised"
    >
      {open ? 'Hide tools' : `Tools (${entry.tools.length})`}
    </button>
  )
}

/** The expanded panel: one switch per tool, each the tool's default for new Claude sessions. */
export function McpToolDefaults({ serverKey, id }: { serverKey: string; id: string }) {
  const scope = useMcpScope()
  const setTool = useSetMcpToolDefault()
  const entry = scope.data?.servers?.[serverKey]
  if (!entry?.tools) return null
  const short = shortName(serverKey)
  return (
    <div id={id} role="group" aria-label={`Tool defaults for ${short}`} className="mt-1 px-3 space-y-1">
      <p className="text-xs text-ink-muted">
        Defaults for new and restarted Claude sessions
        {entry.toolsSeenAt ? ` · Seen ${relativeAge(entry.toolsSeenAt)}` : ''}
      </p>
      <McpToolSwitchList
        server={short}
        tools={entry.tools}
        busy={setTool.isPending}
        pendingTool={setTool.isPending ? setTool.variables?.tool : null}
        onToggle={(tool, enabled) => setTool.mutate({ server: serverKey, tool, enabled })}
      />
      {setTool.isError && (
        <p role="alert" className="text-xs text-danger break-words">
          {mcpMutationErrorMessage(setTool.error, 'Could not change the tool.')}
        </p>
      )}
    </div>
  )
}
