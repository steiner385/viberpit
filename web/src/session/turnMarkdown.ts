import type { Turn } from './reducer'

/**
 * The Markdown a turn's "Copy as Markdown" puts on the clipboard: its text blocks
 * (the assistant's prose, or the user's prompt) joined by a blank line. Tools,
 * thinking, images and notices are excluded. `null` when there is nothing to copy.
 */
export function turnMarkdown(turn: Turn): string | null {
  const parts = turn.blocks.flatMap((b) => (b.kind === 'text' && b.text.trim() ? [b.text] : []))
  return parts.length ? parts.join('\n\n') : null
}
