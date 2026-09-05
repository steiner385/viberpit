import { cn } from '@/lib/utils'
import { stripAnsi, extractText, splitImages } from '@/lib/toolContent'
import { ResultImages } from './ResultImages'
import { StructuredView } from './StructuredView'

interface Props {
  payload: Record<string, unknown>
  ok?: boolean
}

/** Keys past which a flat payload is a record, not a status — render the table. */
const COMPACT_MAX_KEYS = 3

/**
 * A payload small and flat enough to state in one line: no `content` envelope,
 * at most `COMPACT_MAX_KEYS` entries, every value a scalar. Returns null when
 * the structured renderer should take it instead.
 *
 * This is the shape every Codex tool result has — `{status}` for a file change /
 * MCP / dynamic tool call, `{kind}` for sub-agent activity, `{}` for web search,
 * plan and image view (`server/codex/translator.py::_tool_result`) — where a
 * key/value table plus a raw-JSON disclosure is more chrome than content.
 */
function compactEntries(payload: Record<string, unknown>): [string, string][] | null {
  const entries = Object.entries(payload)
  if (entries.length > COMPACT_MAX_KEYS) return null
  if (!entries.every(([, v]) => v === null || ['string', 'number', 'boolean'].includes(typeof v))) {
    return null
  }
  return entries.map(([k, v]) => [k, String(v)])
}

/**
 * The safe default tool-result renderer. Four tiers, raw JSON last:
 *  1. Text     — extract `content` as text, strip ANSI, show as <pre>.
 *  2. Compact  — a payload-free or single-fact result as one line.
 *  3. Structured — object/array content (or the whole payload) as a table / rows.
 *  4. Raw      — the structured renderer always exposes a `raw` escape hatch.
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

  // No usable text and no envelope: a small flat payload states itself in one
  // line rather than as a one-row table with a raw-JSON disclosure.
  if (!hasContent) {
    const compact = compactEntries(payload)
    if (compact) {
      return (
        <div
          data-testid="result-compact"
          className={cn(
            'flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs',
            ok
              ? 'text-gray-600 dark:text-gray-300'
              : 'rounded bg-red-50 p-2 text-red-800 dark:bg-red-950/30 dark:text-red-300',
          )}
        >
          {compact.length === 0 ? (
            <span className="italic opacity-70">no details</span>
          ) : (
            compact.map(([k, v]) => (
              <span key={k} className="inline-flex items-center gap-1">
                <span className="text-gray-400 dark:text-gray-500">{k}</span>
                <span className="font-mono break-all">{v}</span>
              </span>
            ))
          )}
        </div>
      )
    }
  }

  // No usable text: render the structured content (if any) or the whole payload.
  const structured = hasContent && payload.content != null ? payload.content : payload
  return <StructuredView data={structured} ok={ok} />
}
