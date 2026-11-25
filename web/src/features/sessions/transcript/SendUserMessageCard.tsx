import type { ToolCallStatus } from '@/lib/toolContent'
import type { UserMessage } from '@/session/userFacingTools'
import { Markdown } from './Markdown'
import { CallNotice, SentFileList } from './userFacingParts'

/** `SendUserMessage`: the message as assistant prose (same measure), then any attachments. */
export function SendUserMessageCard({ message: { message, attachments }, call }: { message: UserMessage; call: ToolCallStatus }) {
  return (
    <div className="max-w-[70ch]" data-testid="send-user-message-card">
      {message !== null
        ? <Markdown>{message}</Markdown>
        : attachments.length === 0 && <p className="text-sm text-ink-muted italic">(empty message)</p>}
      {call.status === 'error' && <CallNotice call={call} pending="" />}
      <SentFileList files={attachments} inline />
    </div>
  )
}
