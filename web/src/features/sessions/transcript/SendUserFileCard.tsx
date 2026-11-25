import { useContext } from 'react'
import { Paperclip } from 'lucide-react'
import { useSentFiles } from '@/api/sentFilesHooks'
import type { ToolCallStatus } from '@/lib/toolContent'
import type { SentFiles } from '@/session/userFacingTools'
import { worktreeRelativePath } from './filePathRef'
import { TranscriptSessionContext, useFileRefActions } from './fileRefContext'
import { CallNotice, CardHeader, CARD_FRAME_CLASS, SentFileList, type PublishedFiles } from './userFacingParts'

interface Props {
  sent: SentFiles
  call: ToolCallStatus
  /** The `SendUserFile` call — what the owning daemon publishes the files of. */
  toolId: string
}

/**
 * `SendUserFile`: the caption over the delivered files — images inline, the rest
 * as links. A file the browser cannot reach through the worktree (outside it, or
 * no worktree here) is published by the daemon that owns the session (#3487): an
 * image comes back as an attachment URL and renders inline; anything else keeps
 * the "on the session host" notice.
 */
export function SendUserFileCard({ sent: { files, caption, display }, call, toolId }: Props) {
  const actions = useFileRefActions()
  // The live worktree's session, else the archived reader's; none on the sub-agent page.
  const archivedId = useContext(TranscriptSessionContext)
  const sessionId = actions?.sessionId ?? archivedId
  const needsPublish = files.some((path) => !actions || worktreeRelativePath(path, actions.cwd) === null)
  // Only once the call has a result: before that, a live session's tool_use line may
  // not be on disk yet, and the lookup would answer "no such call".
  const settled = call.status !== 'pending'
  const published = useSentFiles(sessionId, toolId, { enabled: needsPublish && settled })
  const publishedFiles: PublishedFiles = {
    pending: needsPublish && settled && !!sessionId && published.isPending,
    byPath: new Map((published.data?.files ?? []).map((f) => [f.path, f])),
  }
  const count = files.length === 0 ? 'No files' : files.length === 1 ? 'Shared a file' : `Shared ${files.length} files`
  return (
    <div className={CARD_FRAME_CLASS} data-testid="send-user-file-card">
      <CardHeader icon={<Paperclip size={14} />}>{caption ?? count}</CardHeader>
      <CallNotice call={call} pending="Sending…" />
      <SentFileList files={files} inline={display === 'render'} published={publishedFiles} />
    </div>
  )
}
