import type { ToolBlock, ToolDetailMode } from '@/session/groupBlocks'
import type { SubThread } from '@/session/reducer'
import { ToolCallCard } from './ToolCallCard'

interface ToolGroupProps {
  tools: ToolBlock[]
  /** True when this group is the newest activity (last group of the last turn). */
  isLiveTail: boolean
  toolDetailMode: ToolDetailMode
  /** Persisted default for whether thinking blocks start collapsed (threaded to nested sub-agent turns). */
  thinkingCollapsed?: boolean
  /** Sub-agent threads keyed by the spawning Task toolId (for nested rendering). */
  subAgents?: Record<string, SubThread>
}

/**
 * Render a run of consecutive tool calls the way Claude Code does: one compact
 * line per tool, never a rolled-up "N tool calls" summary. Each row collapses to
 * an icon · name · key-arg · ✓/✕ · size-hint line and expands on click. The
 * global `toolDetailMode` force-opens/closes every result panel; in 'auto' only
 * the live tail's most recent tool auto-opens its output.
 */
export function ToolGroup({ tools, isLiveTail, toolDetailMode, thinkingCollapsed, subAgents }: ToolGroupProps) {
  return (
    <div className="my-0.5">
      {tools.map((t, i) => (
        <ToolCallCard
          key={t.id}
          block={t}
          density="line"
          toolDetailMode={toolDetailMode}
          thinkingCollapsed={thinkingCollapsed}
          defaultResultOpen={isLiveTail && i === tools.length - 1}
          subThread={subAgents?.[t.toolId]}
        />
      ))}
    </div>
  )
}
