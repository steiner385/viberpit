import { memo } from 'react'
import { User } from 'lucide-react'
import type { Turn as TurnType, SubThread } from '@/session/reducer'
import type { PromptContext } from '@/session/events'
import { groupBlocks, type ToolDetailMode } from '@/session/groupBlocks'
import { useSessions } from '@/api/hooks'
import { navDefForPath } from '@/shell/navDefs'
import { AssistantText } from './AssistantText'
import { UserBubble } from './UserBubble'
import { AutomatedPrompt } from './AutomatedPrompt'
import { ThinkingBlock } from './ThinkingBlock'
import { ToolCallCard } from './ToolCallCard'
import { ToolGroup } from './ToolGroup'
import { QuestionCard } from '@/features/sessions/panels/QuestionCard'
import { ErrorCard } from './ErrorCard'
import { RecoveringCard } from './RecoveringCard'
import { NoticeCard } from './NoticeCard'
import { BlockRow } from './BlockRow'
import { PendingPromptNote } from '@/features/sessions/panels/PendingPromptNote'

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
  /** Mirrors TranscriptView's variant — a noticeCard only renders its rich card in the
   *  assistant drawer; other variants (e.g. the read-only archived reader) fall back to a plain line. */
  variant?: 'session' | 'assistant'
  /** Wired to a noticeCard's "Ask about it" button. Absent in read-only views, where the
   *  button hides (NoticeCard has nothing to hand the prefilled text to). */
  onAsk?: (text: string) => void
}

export const Turn = memo(function Turn({
  turn, isLastTurn, toolDetailMode, thinkingCollapsed, streamingBlockId = null,
  onAnswer, subAgents, sessionModel, variant = 'session', onAsk,
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
      {isUser && turn.context && <PromptContextChip context={turn.context} />}
      {/* Positive attribution for a prompt a person actually typed. The automated
          case carries its own caption inside AutomatedPrompt, so this renders only
          for the human one — "blue bubble on the right" now means "you", and says so
          rather than leaving the reader to infer it from a colour. Turn-level, like
          the context chip, so a multi-block prompt is captioned once. */}
      {isUser && !turn.origin && <YouCaption />}
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
            if (!isUser)
              return <BlockRow key={block.id} ts={rowTs}><AssistantText text={block.text} streaming={block.id === streamingBlockId} /></BlockRow>
            // A prompt nobody typed leaves the right-aligned "you" lane entirely
            // rather than being re-tinted inside it (see AutomatedPrompt).
            return <BlockRow key={block.id} ts={rowTs}>{turn.origin
              ? <AutomatedPrompt origin={turn.origin} text={block.text} />
              : <UserBubble text={block.text} />}</BlockRow>
          case 'image':
            return <BlockRow key={block.id} ts={rowTs}>{turn.origin
              ? <AutomatedPrompt origin={turn.origin} images={block.images} />
              : <UserBubble images={block.images} />}</BlockRow>
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
                <div className="my-2 text-center text-xs text-ink-muted italic">
                  {block.text}
                </div>
              </BlockRow>
            )
          case 'error':
            return <BlockRow key={block.id} ts={rowTs}><ErrorCard code={block.code} message={block.message} resetsAt={block.resetsAt} /></BlockRow>
          case 'recovering':
            return <BlockRow key={block.id} ts={rowTs}><RecoveringCard message={block.message} /></BlockRow>
          case 'noticeCard':
            return (
              <BlockRow key={block.id} ts={rowTs}>
                {variant === 'assistant'
                  ? <NoticeCard notice={block} onAsk={onAsk} />
                  : (
                    <div className="my-2 text-center text-xs text-ink-muted italic">
                      {block.title}
                    </div>
                  )}
              </BlockRow>
            )
          case 'noticeCollapsed':
            return (
              <BlockRow key={block.id} ts={rowTs}>
                <div className="my-2 text-center text-xs text-ink-muted italic">
                  {block.count} older notice{block.count === 1 ? '' : 's'}
                </div>
              </BlockRow>
            )
          default:
            return null
        }
      })}
      {/* A prompt the user has sent that no server echo has confirmed yet. The `user`
          echo adopts the turn and clears `pending`, so this note disappears the moment
          the real turn exists — no separate teardown. */}
      {isUser && turn.pending && <PendingPromptNote stage={turn.pending} />}
    </div>
  )
})

/**
 * Small, low-emphasis chip marking where a prompt was sent from — the AI assistant
 * drawer's composer stamps this (`SessionComposer`'s `contextProvider`). Prefers the
 * open session's title (resolved live via `useSessions()`) over the route label, since
 * "on My PR fix session" is more useful than "on Sessions" while a session is open.
 */
function PromptContextChip({ context }: { context: PromptContext }) {
  const { data: sessions } = useSessions()
  const sessionTitle = context.sessionId
    ? sessions?.find((s) => s.id === context.sessionId)?.name
    : undefined
  const label = sessionTitle ?? navDefForPath(context.route)?.label
  if (!label) return null
  return (
    <div className="flex justify-end pb-1">
      <span className="rounded-full border border-edge px-2 py-0.5 text-2xs text-ink-muted">
        on {label}
      </span>
    </div>
  )
}

/**
 * "You" — the counterpart to `AutomatedPrompt`'s caption. Kept faint and small: it is
 * a reassurance to be scanned past, not a label to be read on every turn. Its value is
 * entirely in the contrast with an automated prompt sitting a few rows away.
 */
function YouCaption() {
  return (
    <div className="flex justify-end pb-1">
      <span className="inline-flex items-center gap-1 text-2xs text-ink-faint">
        <User size={10} aria-hidden="true" />
        You
      </span>
    </div>
  )
}
