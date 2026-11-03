/** The rewind toasts (D-9), on the lazy chunk beside the copy they read. */
import { toast } from 'sonner'
import * as copy from './rewindCopy'

/** One error toast: `code`'s copy from `rewindCopy`, else the server's `message`, else
 *  the fallback. The caller reads both off the error (`useRewind:failRewind`), so this
 *  chunk needs none of the API modules. */
export function toastRewindError(code: string | null, message: string, undo: boolean): void {
  const mapped = code !== null ? copy.REWIND_ERROR_MESSAGES[code] : undefined
  toast.error(mapped ?? (message || (undo ? copy.UNDO_FAILED_FALLBACK : copy.REWIND_FAILED_FALLBACK)))
}

/** An undo that left some paths alone lists them. */
export function toastUndoSkipped(paths: readonly string[]): void {
  toast(copy.undoSkipped(paths))
}

/** `/rewind` that opens nothing: busy, or no prompt to rewind to. */
export function toastNoPicker(busy: boolean): void {
  toast(busy ? copy.REWIND_ERROR_MESSAGES.REWIND_BUSY : copy.REWIND_NOTHING)
}
