import { memo } from 'react'
import type { Turn as TurnType, SubThread } from '@/session/reducer'
import { groupBlocks, type ToolDetailMode } from '@/session/groupBlocks'
import { AssistantText } from './AssistantText'
import { UserBubble } from './UserBubble'
import { ThinkingBlock } from './ThinkingBlock'
import { ToolCallCard } from './ToolCallCard'
import { ToolGroup } from './ToolGroup'
import { QuestionCard } from './QuestionCard'
import { ErrorCard } from './ErrorCard'
import { BlockRow } from './BlockRow'

interface Props {
  turn: TurnType
  isLastTurn: boolean
  toolDetailMode: ToolDetailMode
  thinkingCollapsed: boolean
  /** Id of the block currently being streamed live (renders as plain text);
   *  null/absent means every text block is finalized and rendered as markdown. */
  streamingBlockId?: string | null
  onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void
  subAgents?: Record<string, SubThread>
  /** The session's current model — threaded down to badge subagents on a different model. */
  sessionModel?: string | null
}

export const Turn = memo(function Turn({
  turn, isLastTurn, toolDetailMode, thinkingCollapsed, streamingBlockId = null,
  onAnswer, subAgents, sessionModel,
}: Props) {
  const isUser = turn.role === 'user'
  // Suppress live questions and open permissions inline — they are rendered by the
  // pinned PendingQuestionPanel / PendingPermissionPanel above the composer, where the
  // controls stay reachable regardless of scroll. Historical questions stay; resolved
  // permissions are removed from state entirely, so every permission block here is open.
  const visibleBlocks = turn.blocks.filter(
    (b) => !(b.kind === 'question' && b.live) && b.kind !== 'permission',
  )
  const items = groupBlocks(visibleBlocks)
  const lastIdx = items.length - 1

  // An empty turn (e.g. a turnStart with no content yet, or a turn whose only
  // tool was a skipped AskUserQuestion) must not render a padded gap.
  if (items.length === 0) return null

  return (
    <div className="px-4 py-2">
      {items.map((item, idx) => {
        const rowTs = item.kind === 'toolGroup' ? item.tools[0]?.ts : item.block.ts

        if (item.kind === 'toolGroup') {
          // The live tail is the final render item of the last turn.
          const isLiveTail = isLastTurn && idx === lastIdx
          return (
            <BlockRow key={item.tools[0].id} ts={rowTs}>
              <ToolGroup
                tools={item.tools}
                isLiveTail={isLiveTail}
                toolDetailMode={toolDetailMode}
                thinkingCollapsed={thinkingCollapsed}
                subAgents={subAgents}
                sessionModel={sessionModel}
              />
            </BlockRow>
          )
        }

        const block = item.block
        switch (block.kind) {
          case 'text':
            return isUser
              ? <BlockRow key={block.id} ts={rowTs}><UserBubble text={block.text} /></BlockRow>
              : <BlockRow key={block.id} ts={rowTs}><AssistantText text={block.text} streaming={block.id === streamingBlockId} /></BlockRow>
          case 'image':
            return <BlockRow key={block.id} ts={rowTs}><UserBubble images={block.images} /></BlockRow>
          case 'thinking':
            return <BlockRow key={block.id} ts={rowTs}><ThinkingBlock text={block.text} defaultCollapsed={thinkingCollapsed} /></BlockRow>
          case 'tool':
            // Unreachable: tool blocks are coalesced into toolGroup items above.
            return <BlockRow key={block.id} ts={rowTs}><ToolCallCard block={block} density="line" thinkingCollapsed={thinkingCollapsed} subThread={subAgents?.[block.toolId]} sessionModel={sessionModel} /></BlockRow>
          case 'question':
            // A LIVE open question is rendered by the pinned PendingQuestionPanel
            // above the composer — omit it inline to avoid a duplicate. Historical
            // (replayed) questions render inline, read-only.
            return <BlockRow key={block.id} ts={rowTs}><QuestionCard block={block} onAnswer={onAnswer} readOnly /></BlockRow>
          case 'notice':
            return (
              <BlockRow key={block.id} ts={rowTs}>
                <div className="my-2 text-center text-xs text-gray-400 dark:text-gray-500 italic">
                  {block.text}
                </div>
              </BlockRow>
            )
          case 'error':
            return <BlockRow key={block.id} ts={rowTs}><ErrorCard code={block.code} message={block.message} resetsAt={block.resetsAt} /></BlockRow>
          default:
            return null
        }
      })}
    </div>
  )
})
