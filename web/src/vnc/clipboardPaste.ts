// Paste the client's clipboard into the focused remote X session.
//
// Implemented as keystroke relay (not noVNC's clipboardPasteFrom): typing the
// text lands it directly in the focused remote field even when the remote X
// session has no clipboard manager — the password-into-the-login-window case.
// Reuses relayInput so the codepoint→keysym mapping has a single source of truth.
import { relayInput, type SendKey } from './keyRelay'

export type PasteResult = 'pasted' | 'empty' | 'denied'

/**
 * Read the client clipboard via `readText` and type it into the session.
 * `readText` is injected (rather than calling navigator.clipboard directly) so
 * the behaviour — including the permission-denied path — is unit-testable.
 *
 * - 'denied': reading the clipboard threw (permission blocked / no clipboard API)
 * - 'empty':  clipboard had no text (nothing relayed)
 * - 'pasted': text was relayed into the session
 */
export async function pasteClipboardToSession(
  readText: () => Promise<string>,
  send: SendKey,
): Promise<PasteResult> {
  let text: string
  try {
    text = await readText()
  } catch {
    return 'denied'
  }
  if (!text) return 'empty'
  relayInput(text, send)
  return 'pasted'
}
