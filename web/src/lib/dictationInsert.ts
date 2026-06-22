/**
 * Remove `removeLen` characters at `anchor` in `current` and splice in `insert`.
 * Used to replace a live interim transcript chunk with a longer interim/final one,
 * keeping dictated text anchored at the caret position where dictation began.
 */
export function spliceAt(current: string, anchor: number, removeLen: number, insert: string): string {
  return current.slice(0, anchor) + insert + current.slice(anchor + removeLen)
}
