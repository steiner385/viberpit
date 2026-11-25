import { createContext, useContext } from 'react'

/**
 * What a transcript path link can do (D-7), provided by the LIVE session's
 * `SessionBody` when the session has a `cwd`. With no provider — the archived
 * reader, the sub-agent page, the assistant drawer — a path stays click-to-copy,
 * a markdown link to a local path is plain text, and a local image is a notice.
 *
 * A context rather than props because the path renderer sits at the bottom of
 * the shared transcript tree (`TranscriptView` → `Turn` → … → `MarkdownDoc`),
 * and only one of its four hosts has a worktree to open.
 */
export interface FileRefActions {
  /** The live session whose worktree `cwd` is — what an in-worktree image is read from. */
  sessionId: string
  /** The session's worktree root; a path under it becomes a Files-tab link. */
  cwd: string
  /** Open the Files tab at a worktree-relative `path` (and 1-based `line`). */
  openFile: (path: string, line: number | null) => void
  /** Insert `@path` into the session composer's draft. Absent without a composer. */
  attachMention?: (path: string) => void
}

export const FileRefContext = createContext<FileRefActions | null>(null)

export function useFileRefActions(): FileRefActions | null {
  return useContext(FileRefContext)
}

/**
 * The id of the archived session whose transcript the read-only reader shows —
 * the one host with a session but no `FileRefContext` (no live worktree). It lets
 * a `SendUserFile` card there ask the owning daemon to publish its files (#3487);
 * the live view needs none, its `FileRefActions.sessionId` is the same id. Here,
 * beside `FileRefContext`, so the lazy card reads it without splitting another
 * eager module into a preloaded chunk.
 */
export const TranscriptSessionContext = createContext<string | null>(null)
