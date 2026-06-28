// Pure translation from DOM keyboard signals to noVNC sendKey() calls.
// Self-contained ON PURPOSE: @novnc/novnc's "exports" seals every subpath, so
// noVNC's own keysymdef table cannot be imported here.

export type SendKey = (keysym: number, code: string | null, down?: boolean) => void

// X11 keysyms for the control keys a soft keyboard emits via keydown.
export const XK_RETURN = 0xff0d
export const XK_BACKSPACE = 0xff08

/**
 * Translate a Unicode code point to an X11 keysym, reproducing noVNC's lookup()
 * rule (we can't import theirs):
 *   - Latin-1 (0x20..0xff): the keysym IS the code point.
 *   - everything else: the Unicode keysym form, 0x01000000 | codePoint.
 * (The legacy non-Unicode keysym table is intentionally skipped — YAGNI for
 *  mobile text entry; modern X/VNC accept the Unicode form.)
 *
 */
export function charToKeysym(codepoint: number): number {
  // Latin-1 is a one-to-one mapping in X11 keysyms.
  if (codepoint >= 0x20 && codepoint <= 0xff) return codepoint
  // Everything else: the Unicode keysym form modern X/VNC accept as a fallback.
  return 0x01000000 | codepoint
}

/** Relay typed text: one press+release per character (down omitted). */
export function relayInput(text: string, send: SendKey): void {
  for (const ch of text) {
    const cp = ch.codePointAt(0)
    if (cp === undefined) continue
    send(charToKeysym(cp), null)
  }
}

/** Relay control keys from keydown. Returns true if handled (caller preventDefaults). */
export function relayKeydown(e: Pick<KeyboardEvent, 'key' | 'code'>, send: SendKey): boolean {
  if (e.key === 'Enter') {
    send(XK_RETURN, e.code)
    return true
  }
  if (e.key === 'Backspace') {
    send(XK_BACKSPACE, e.code)
    return true
  }
  return false
}
