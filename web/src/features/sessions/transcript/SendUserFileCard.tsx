import { Paperclip } from 'lucide-react'
import type { ToolCallStatus } from '@/lib/toolContent'
import type { SentFiles } from '@/session/userFacingTools'
import { CallNotice, CardHeader, CARD_FRAME_CLASS, SentFileList } from './userFacingParts'

/** `SendUserFile`: the caption over the delivered files — images inline, the rest as links. */
export function SendUserFileCard({ sent: { files, caption, display }, call }: { sent: SentFiles; call: ToolCallStatus }) {
  const count = files.length === 0 ? 'No files' : files.length === 1 ? 'Shared a file' : `Shared ${files.length} files`
  return (
    <div className={CARD_FRAME_CLASS} data-testid="send-user-file-card">
      <CardHeader icon={<Paperclip size={14} />}>{caption ?? count}</CardHeader>
      <CallNotice call={call} pending="Sending…" />
      <SentFileList files={files} inline={display === 'render'} />
    </div>
  )
}
