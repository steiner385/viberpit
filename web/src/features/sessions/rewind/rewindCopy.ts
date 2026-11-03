/**
 * What "Rewind to here" (D-9) says — THE one table of rewind copy. Pure data with no
 * imports, loaded on first use (`rewindFlow`) so it stays off the entry chunk. The menu
 * labels, the picker's name and the divider's line live beside their components, which
 * are on screen before any rewind runs.
 *
 * The codes are the server's (`server/sessions/rewind.py:REWIND_CODES` and
 * `daemon_outdated`); a code missing here falls back to the server's own message.
 */

/** Under every confirm: what a rewind can't put back. */
export const REWIND_CAVEAT =
  'Changes made by shell commands are not restored, and edits made outside Claude since that message are overwritten.'

/** The prompt has no file checkpoint (sent before checkpointing was on): conversation only. */
export const REWIND_CONVERSATION_ONLY =
  "Files can't be restored for this message — only the conversation will be rewound."

export const REWIND_TITLE = 'Rewind to this message?'
export const EDIT_TITLE = 'Rewind and edit this message?'
export const REWIND_CONFIRM = 'Rewind'
export const EDIT_CONFIRM = 'Rewind and edit'
export const RETRY_TITLE = 'Rewind and retry this message?'
export const RETRY_CONFIRM = 'Rewind and retry'
/** Rewind & retry: the resend is a new prompt, which expires the rewind's undo. */
export const RETRY_NOTE = "Your message is sent again right after the rewind, so this rewind can't be undone."
export const REWIND_LOADING = 'Checking what this rewind restores…'
export const REWIND_NO_FILES = 'No files change.'
/** Edit & resend over a composer that already holds text. */
export const REWIND_REPLACES_DRAFT = 'Your unsent draft will be replaced.'

/** `/rewind` with no prompt to rewind to. */
export const REWIND_NOTHING = 'Nothing to rewind to yet'

/** An undo that left some paths alone (a symlink on the path is never followed). */
export const undoSkipped = (paths: readonly string[]) =>
  `Rewind undone — these files were left as they are: ${paths.join(', ')}`

/** A failure whose body carries no usable message. */
export const REWIND_FAILED_FALLBACK = "Couldn't rewind this session"
export const UNDO_FAILED_FALLBACK = "Couldn't undo the rewind"

/** Undo's `REWIND_FAILED`: some files could not be restored; the backup is kept. */
export const undoFailed = (paths: readonly string[]) =>
  "Couldn't finish undoing — your backup is kept; try Undo again"
  + (paths.length ? `. Not restored: ${paths.join(', ')}` : '')

export const REWIND_ERROR_MESSAGES: Readonly<Record<string, string>> = {
  REWIND_UNAVAILABLE: "This session can't be rewound right now",
  REWIND_BUSY: "Claude is working or waiting on you — rewind once that's settled",
  REWIND_NO_CHECKPOINT: "Files can't be restored for this message",
  REWIND_STALE_TARGET: 'The session changed since this view loaded — reload it and try again',
  REWIND_TARGET_NOT_FOUND: "That message isn't in the session's current conversation",
  REWIND_SPLITS_TOOL_CALL: "Can't rewind here — it would split a tool call from its result",
  REWIND_NOT_USER_MESSAGE: 'Only your own messages can be rewound to',
  REWIND_TOO_LARGE: 'Too many changed files to back up for undo, so nothing was rewound',
  REWIND_UNDO_EXPIRED: "This rewind can't be undone any more — a message was sent since",
  REWIND_FAILED: "Couldn't finish the rewind — some files may not have been restored",
  daemon_outdated: 'The session runner needs an update before it can rewind — try after the next rollout',
}
