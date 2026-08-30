import { cn } from '@/lib/utils'
import { stripAnsi, extractText, splitImages } from '@/lib/toolContent'
import { ResultImages } from './ResultImages'
import { StructuredView } from './StructuredView'

interface Props {
  payload: Record<string, unknown>
  ok?: boolean
}

/**
 * The safe default tool-result renderer. Three tiers, raw JSON last:
 *  1. Text     — extract `content` as text, strip ANSI, show as <pre>.
 *  2. Structured — object/array content (or the whole payload) as a table / rows.
 *  3. Raw      — the structured renderer always exposes a `raw` escape hatch.
 *
 * This replaces the old behavior of dumping the `{ "content": … }` envelope as
 * JSON, which leaked raw JSON for every search / mcp / unknown tool.
 */
export function GenericResult({ payload, ok = true }: Props) {
  const hasContent = 'content' in payload
  // Image blocks (e.g. an MCP screenshot tool) render as thumbnails; text is
  // extracted from the remaining blocks so a rendered image isn't also labelled.
  const { images, rest } = splitImages(hasContent ? payload.content : null)
  const text = hasContent ? extractText(rest) : null

  if (text != null) {
    const cleaned = stripAnsi(text)
    if (cleaned.trim() !== '' || images.length > 0) {
      return (
        <div>
          {cleaned.trim() !== '' && (
            <pre
              className={cn(
                'max-h-64 overflow-auto rounded p-2 text-xs font-mono md:max-h-96',
                ok
                  ? 'bg-gray-50 dark:bg-gray-900 text-gray-800 dark:text-gray-200'
                  : 'bg-red-50 dark:bg-red-950/30 text-red-800 dark:text-red-300',
              )}
            >
              {cleaned}
            </pre>
          )}
          <ResultImages images={images} />
        </div>
      )
    }
  }

  // No usable text: render the structured content (if any) or the whole payload.
  const structured = hasContent && payload.content != null ? payload.content : payload
  return <StructuredView data={structured} ok={ok} />
}
