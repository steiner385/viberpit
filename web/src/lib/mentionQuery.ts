/**
 * Finds the active `@mention` query at the caret, or null when the caret isn't
 * inside one. A mention starts at an `@` preceded by start-of-string or whitespace
 * and runs to the caret with no whitespace in between — so it can open mid-message
 * (unlike the slash-command palette, which only ever anchors at position 0).
 *
 * An `@` inside code — an open fenced block or an open inline code span — is code,
 * not a mention (`decorator @x`, `user@host`), so it never opens the menu.
 */
export function mentionQueryAt(text: string, caret: number): string | null {
  let i = caret - 1
  while (i >= 0 && text[i] !== '@' && !/\s/.test(text[i])) i--
  if (i < 0 || text[i] !== '@') return null
  if (i > 0 && !/\s/.test(text[i - 1])) return null
  if (insideCode(text.slice(0, i))) return null
  return text.slice(i + 1, caret)
}

/** Whether the end of `before` sits inside an unclosed ``` fence or `inline` span. */
function insideCode(before: string): boolean {
  const fences = before.split('```')
  if (fences.length % 2 === 0) return true
  const line = fences[fences.length - 1].split('\n').pop() ?? ''
  return (line.match(/`/g)?.length ?? 0) % 2 === 1
}

/**
 * The `@` token for a worktree file: `@<path>`, or `@"<path>"` when the path holds
 * whitespace — Claude Code's own parser reads `@"([^"]+)"` (and its file completer
 * inserts that form), while a bare `@` token ends at the first space. The one
 * spelling for both the composer's `@` menu and a transcript path's "Attach as @".
 */
export function fileMentionToken(path: string): string {
  return /\s/.test(path) ? `@"${path}"` : `@${path}`
}
