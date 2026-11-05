/**
 * Rail card: what is filling (or bounding) the session — the context-window
 * meter (same numbers and wording as the prompt row's ring), a Compact now
 * action as auto-compaction nears, the memory files the CLI loaded (D-13), the
 * MCP servers inline, the standing Always-allow rules, and the account's quota
 * windows.
 */
import { lazy, Suspense, useState } from 'react'
import type { McpServerState, UsageWindow } from '@/api/types'
import type { Command, MemoryFile } from '@/session/events'
import { cn } from '@/lib/utils'
import { contextSeverity, contextUsedLabel, type ContextSeverity } from '@/lib/contextWindow'
import { quotaWindowAriaLabel, quotaWindowTitle, windowLabel } from '@/lib/usageWindows'
import { Button } from '@/ui/Button'
import { TOUCH_FLOOR } from '@/ui/list/listControlStyles'
import { DeferredMcpInlineList } from '../DeferredMcpStatusReadout'
import { SessionRulesReadout } from '../SessionRulesReadout'
import { RailCard } from './RailCard'
import { worktreeRelativePath } from '../transcript/filePathRef'
import { memoryTypeLabel } from './memoryFiles'

// Lazy: the viewer (and the Files tab frame it reuses) loads when a file is opened.
const MemoryFileDialog = lazy(() => import('./MemoryFileDialog'))

/** Worktree-relative inside the worktree, absolute otherwise (the browser has no HOME for `~/`). */
function shownPath(path: string, cwd: string | null): string {
  return (cwd ? worktreeRelativePath(path, cwd) : null) ?? path
}

const METER_FILL: Record<ContextSeverity, string> = {
  neutral: 'bg-accent',
  warning: 'bg-warning',
  danger: 'bg-danger',
}

interface Props {
  sessionId: string
  account: string
  /** Remaining context, 0–100 (`rt.contextPct`); null before any usage is known. */
  contextPct: number | null
  /** The account's quota windows (`rt.quota`). */
  quota: UsageWindow[] | null
  /** The engine's command catalog (`rt.commands`): Compact now needs `compact`. */
  commands: Command[]
  /** Live MCP status (`rt.state.mcpServers`); null falls back to the GET snapshot. */
  mcpLive: McpServerState[] | null
  /** Offer per-tool MCP switches: the engine's `flagSettings` capability (false for Codex; D-16b). */
  mcpToolSwitches?: boolean
  /** The memory files the CLI loaded, in its order (`rt.memoryFiles`); null or empty hides the list. */
  memoryFiles: MemoryFile[] | null
  /** `session.cwd` — the worktree a file path is shown relative to; null shows paths absolute. */
  cwd: string | null
  /** Send `/compact` as a plain prompt. The card never touches the runtime itself. */
  onCompact: () => void
}

export function ContextCard({
  sessionId, account, contextPct, quota, commands, mcpLive, mcpToolSwitches = false, memoryFiles, cwd, onCompact,
}: Props) {
  const severity = contextSeverity(contextPct)
  const canCompact = commands.some((c) => c.name === 'compact')
  const [openFile, setOpenFile] = useState<MemoryFile | null>(null)

  return (
    <RailCard title="Context">
      {contextPct != null && (
        <div className="flex flex-col gap-1">
          <p className="text-ink">{contextUsedLabel(contextPct)}</p>
          <div
            role="meter"
            aria-label={contextUsedLabel(contextPct)}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={100 - contextPct}
            className="h-1.5 w-full overflow-hidden rounded-pill bg-edge"
          >
            <div className={cn('h-full rounded-pill', METER_FILL[severity])} style={{ width: `${100 - contextPct}%` }} />
          </div>
          {severity === 'danger' && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium text-danger">Auto-compacts soon</span>
              {canCompact && (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={onCompact}
                  className={TOUCH_FLOOR}
                >
                  Compact now
                </Button>
              )}
            </div>
          )}
        </div>
      )}

      {memoryFiles && memoryFiles.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="text-ink-muted">Memory</span>
          <ul aria-label="Memory files" className="flex min-w-0 flex-col">
            {memoryFiles.map((f, i) => {
              const label = memoryTypeLabel(f.type)
              const shown = shownPath(f.path, cwd)
              const tokens = f.tokens.toLocaleString('en-US')
              return (
                // The CLI can load one path twice (as itself and as an import), so the index is part of the key.
                <li key={`${i}:${f.path}`} className="min-w-0">
                  <button
                    type="button"
                    aria-label={`${label} ${shown}, ${tokens} tokens`}
                    onClick={() => setOpenFile(f)}
                    className={cn(
                      'flex w-full min-w-0 items-center gap-2 rounded px-1 text-left hover:bg-surface-2',
                      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
                      TOUCH_FLOOR,
                    )}
                  >
                    <span className="shrink-0 rounded bg-surface-2 px-1 text-ink-muted">{label}</span>
                    {/* Cut from the START so the file name stays: the RTL box puts the
                        ellipsis on the left, the LTR span keeps the path's own order. */}
                    <span dir="rtl" className="min-w-0 flex-1 truncate text-left font-mono text-ink" title={f.path}>
                      <span dir="ltr">{shown}</span>
                    </span>
                    <span className="shrink-0 tabular-nums text-ink-muted">{tokens} tokens</span>
                  </button>
                </li>
              )
            })}
          </ul>
        </div>
      )}
      {openFile && (
        <Suspense fallback={null}>
          <MemoryFileDialog
            sessionId={sessionId}
            file={openFile}
            displayPath={shownPath(openFile.path, cwd)}
            onClose={() => setOpenFile(null)}
          />
        </Suspense>
      )}

      <DeferredMcpInlineList sessionId={sessionId} account={account} live={mcpLive} toolSwitches={mcpToolSwitches} />

      <SessionRulesReadout sessionId={sessionId} />

      {quota && quota.length > 0 && (
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="text-ink-muted">Quota</span>
          <ul aria-label="Account quota" className="flex flex-wrap gap-x-3 gap-y-1">
            {quota.map((w) => (
              <li
                key={w.key}
                className="tabular-nums text-ink"
                title={quotaWindowTitle(w)}
                aria-label={quotaWindowAriaLabel(w)}
              >
                {windowLabel(w)} {w.pct}%
              </li>
            ))}
          </ul>
        </div>
      )}
    </RailCard>
  )
}
