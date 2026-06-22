import { memo } from 'react'
import type { Turn as TurnType, SubThread } from '@/session/reducer'
import { AssistantText } from './AssistantText'
import { UserBubble } from './UserBubble'
import { ThinkingBlock } from './ThinkingBlock'
import { ToolCallCard } from './ToolCallCard'
import { PermissionCard } from './PermissionCard'
import { QuestionCard } from './QuestionCard'

// Streaming decision: v1 uses streaming={false} for all assistant text blocks
// (renders as markdown). The reducer tracks openBlockId for future use, but
// threading streamingBlockId through the component tree is deferred to v2.
// This keeps the component simple and avoids prop-drilling complexity.

interface Props {
  turn: TurnType
  permissionMode: string
  onDecision: (permId: string, decision: 'allow' | 'deny' | 'always') => void
  onAnswer: (questionId: string, selected: string[]) => void
  subAgents?: Record<string, SubThread>
}

export const Turn = memo(function Turn({ turn, permissionMode, onDecision, onAnswer, subAgents }: Props) {
  const isUser = turn.role === 'user'

  return (
    <div className="px-4 py-2">
      {turn.blocks.map((block) => {
        switch (block.kind) {
          case 'text':
            return isUser
              ? <UserBubble key={block.id} text={block.text} />
              : <AssistantText key={block.id} text={block.text} streaming={false} />

          case 'thinking':
            return <ThinkingBlock key={block.id} text={block.text} />

          case 'tool':
            return <ToolCallCard key={block.id} block={block} subThread={subAgents?.[block.toolId]} />

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
