import { memo } from 'react'
import type { Turn as TurnType } from '@/session/reducer'
import { groupBlocks, type ToolDetailMode } from '@/session/groupBlocks'
import { AssistantText } from './AssistantText'
import { UserBubble } from './UserBubble'
import { ThinkingBlock } from './ThinkingBlock'
import { ToolCallCard } from './ToolCallCard'
import { ToolGroup } from './ToolGroup'
import { PermissionCard } from './PermissionCard'
import { QuestionCard } from './QuestionCard'

interface Props {
  turn: TurnType
  permissionMode: string
  isLastTurn: boolean
  toolDetailMode: ToolDetailMode
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[]) => void
}

export const Turn = memo(function Turn({
  turn, permissionMode, isLastTurn, toolDetailMode, onDecision, onAnswer,
}: Props) {
  const isUser = turn.role === 'user'
  const items = groupBlocks(turn.blocks)
  const lastIdx = items.length - 1

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
            />
          )
        }

        const block = item.block
        switch (block.kind) {
          case 'text':
            return isUser
              ? <UserBubble key={block.id} text={block.text} />
              : <AssistantText key={block.id} text={block.text} streaming={false} />
          case 'thinking':
            return <ThinkingBlock key={block.id} text={block.text} />
          case 'tool':
            // Unreachable: tool blocks are coalesced into toolGroup items above.
            return <ToolCallCard key={block.id} block={block} density="line" />
          case 'permission':
            return <PermissionCard key={block.id} block={block} mode={permissionMode} onDecision={onDecision} />
          case 'question':
            return <QuestionCard key={block.id} block={block} onAnswer={onAnswer} />
          default:
            return null
        }
      })}
    </div>
  )
})
