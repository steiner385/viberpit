/**
 * Returns whether the caret is on the first and/or last line of a textarea value.
 * Used for boundary-aware ↑/↓ navigation: we only navigate the session list when
 * moving the cursor would be a no-op (already at the line edge).
 */
export function caretLineEdges(
  value: string,
  caret: number,
): { atFirstLine: boolean; atLastLine: boolean } {
  const firstNewline = value.indexOf('\n')
  const atFirstLine = firstNewline === -1 || caret <= firstNewline
  const lastNewline = value.lastIndexOf('\n')
  const atLastLine = lastNewline === -1 || caret > lastNewline
  return { atFirstLine, atLastLine }
}
