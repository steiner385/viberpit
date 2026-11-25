import type { ReactNode } from 'react'
import type { IngestedSentFile } from '@/api/types'
import { isRasterImagePath, type ToolCallStatus } from '@/lib/toolContent'
import { CopyablePath } from './CopyablePath'
import { PathRef } from './PathRef'
import { IMAGE_NOTICE_CLASS, ResultImage } from './ResultImages'
import { WorktreeImage } from './WorktreeImage'
export { CARD_FRAME_CLASS } from './ResultImages'
import { worktreeRelativePath } from './filePathRef'
import { useFileRefActions } from './fileRefContext'

/** Icon + title line of a user-facing tool card. */
export function CardHeader({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <div className="flex items-start gap-2">
      <span className="mt-0.5 shrink-0 text-ink-muted" aria-hidden="true">{icon}</span>
      <div className="min-w-0 flex-1 font-medium text-ink">{children}</div>
    </div>
  )
}

/** The call's delivery state: a note while it has no result, the refusal once it failed. */
export function CallNotice({ call, pending }: { call: ToolCallStatus; pending: string }) {
  if (call.status === 'pending') return <p className="mt-1 text-ink-muted">{pending}</p>
  if (call.status === 'error') return <p className="mt-1 text-danger" role="status">Not delivered: {call.error}</p>
  return null
}

function basename(path: string): string {
  return path.slice(path.lastIndexOf('/') + 1) || path
}

/** What the owning daemon published of a `SendUserFile` call (#3487), by path as written. */
export interface PublishedFiles {
  /** The publish request is still in flight. */
  pending: boolean
  byPath: ReadonlyMap<string, IngestedSentFile>
}

/**
 * Files a session sent the user (`SendUserFile` files, `SendUserMessage`
 * attachments), each where the browser can reach it: an image under the live
 * session's worktree inline (when `inline`), any other worktree file a Files
 * link, a file the owning daemon `published` (an image outside the worktree) from
 * its attachment URL, and anything else — outside the worktree, or no worktree
 * provider at all — named plainly as a file on the session host.
 */
export function SentFileList({ files, inline, published }: { files: string[]; inline: boolean; published?: PublishedFiles }) {
  if (files.length === 0) return null
  return (
    <ul className="mt-2 space-y-2">
      {files.map((path, i) => <li key={`${i}:${path}`}><SentFile path={path} inline={inline} published={published} /></li>)}
    </ul>
  )
}

function SentFile({ path, inline, published }: { path: string; inline: boolean; published?: PublishedFiles }) {
  const actions = useFileRefActions()
  const rel = actions ? worktreeRelativePath(path, actions.cwd) : null
  if (rel === null) {
    const url = published?.byPath.get(path)?.url
    if (url) {
      return inline
        ? <ResultImage url={url} name={basename(path)} />
        : <a href={url} download={basename(path)} className="font-medium text-accent hover:underline">{basename(path)}</a>
    }
    if (published?.pending && isRasterImagePath(path)) return <span className={IMAGE_NOTICE_CLASS}>Loading image…</span>
    return (
      <p className="text-ink-muted">
        <CopyablePath text={path} /> is {actions ? 'on the session host, outside its worktree' : 'a file on the session host'} — it can't be opened here.
      </p>
    )
  }
  if (inline && isRasterImagePath(path)) return <WorktreeImage path={path} alt={basename(path)} />
  return <PathRef text={path} />
}
