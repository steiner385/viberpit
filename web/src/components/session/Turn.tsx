import { memo } from 'react'
import type { Turn as TurnType, SubThread } from '@/session/reducer'
import { groupBlocks, type ToolDetailMode } from '@/session/groupBlocks'
import { AssistantText } from './AssistantText'
import { UserBubble } from './UserBubble'
import { ThinkingBlock } from './ThinkingBlock'
import { ToolCallCard } from './ToolCallCard'
import { ToolGroup } from './ToolGroup'
import { PermissionCard } from './PermissionCard'
import { QuestionCard } from './QuestionCard'
import { ErrorCard } from './ErrorCard'

interface Props {
  turn: TurnType
  permissionMode: string
  isLastTurn: boolean
  toolDetailMode: ToolDetailMode
  thinkingCollapsed: boolean
  /** Id of the block currently being streamed live (renders as plain text);
   *  null/absent means every text block is finalized and rendered as markdown. */
  streamingBlockId?: string | null
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[], mode?: 'option' | 'freeform' | 'chat') => void
  subAgents?: Record<string, SubThread>
}

export const Turn = memo(function Turn({
  turn, permissionMode, isLastTurn, toolDetailMode, thinkingCollapsed, streamingBlockId = null,
  onDecision, onAnswer, subAgents,
}: Props) {
  const isUser = turn.role === 'user'
  // Suppress live questions inline — they are rendered by the pinned
  // PendingQuestionPanel above the composer. Historical questions stay.
  const visibleBlocks = turn.blocks.filter((b) => !(b.kind === 'question' && b.live))
  const items = groupBlocks(visibleBlocks)
  const lastIdx = items.length - 1

  // An empty turn (e.g. a turnStart with no content yet, or a turn whose only
  // tool was a skipped AskUserQuestion) must not render a padded gap.
  if (items.length === 0) return null

  return (
    <div className="px-4 py-2">
      {items.map((item, idx) => {
        if (item.kind === 'toolGroup') {
          // The live tail is the final render item of the last turn.
          const isLiveTail = isLastTurn && idx === lastIdx
          return (
            <ToolGroup
              key={item.tools[0].id}
              tools={item.tools}
              isLiveTail={isLiveTail}
              toolDetailMode={toolDetailMode}
              thinkingCollapsed={thinkingCollapsed}
              subAgents={subAgents}
            />
          )
        }

        const block = item.block
        switch (block.kind) {
          case 'text':
            return isUser
              ? <UserBubble key={block.id} text={block.text} />
              : <AssistantText key={block.id} text={block.text} streaming={block.id === streamingBlockId} />
          case 'image':
            return <UserBubble key={block.id} images={block.images} />
          case 'thinking':
            return <ThinkingBlock key={block.id} text={block.text} defaultCollapsed={thinkingCollapsed} />
          case 'tool':
            // Unreachable: tool blocks are coalesced into toolGroup items above.
            return <ToolCallCard key={block.id} block={block} density="line" thinkingCollapsed={thinkingCollapsed} subThread={subAgents?.[block.toolId]} />
          case 'permission':
            return <PermissionCard key={block.id} block={block} mode={permissionMode} onDecision={onDecision} />
          case 'question':
            // A LIVE open question is rendered by the pinned PendingQuestionPanel
            // above the composer — omit it inline to avoid a duplicate. Historical
            // (replayed) questions render inline, read-only.
            return <QuestionCard key={block.id} block={block} onAnswer={onAnswer} readOnly />
          case 'notice':
            return (
              <div key={block.id} className="my-2 text-center text-xs text-gray-400 dark:text-gray-500 italic">
                {block.text}
              </div>
            )
          case 'error':
            return <ErrorCard key={block.id} code={block.code} message={block.message} resetsAt={block.resetsAt} />
          default:
            return null
        }
      })}
    </div>
  )
})
