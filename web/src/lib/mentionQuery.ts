/**
 * Finds the active `@mention` query at the caret, or null when the caret isn't
 * inside one. A mention starts at an `@` preceded by start-of-string or whitespace
 * and runs to the caret with no whitespace in between — so it can open mid-message
 * (unlike the slash-command palette, which only ever anchors at position 0).
 */
export function mentionQueryAt(text: string, caret: number): string | null {
  let i = caret - 1
  while (i >= 0 && text[i] !== '@' && !/\s/.test(text[i])) i--
  if (i < 0 || text[i] !== '@') return null
  if (i > 0 && !/\s/.test(text[i - 1])) return null
  return text.slice(i + 1, caret)
}
