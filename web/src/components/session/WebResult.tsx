import { stripAnsi, extractText } from '@/lib/toolContent'
import { Markdown } from './Markdown'
import { GenericResult } from './GenericResult'

interface Props {
  input: Record<string, unknown>
  payload: Record<string, unknown>
  ok?: boolean
  /** WebFetch / WebSearch — selects the URL vs query header. */
  name?: string
}

/** WebFetch / WebSearch result: a header showing what was fetched/searched,
 * then the body content rendered as Markdown. */
export function WebResult({ input, payload, ok = true, name }: Props) {
  const url = typeof input.url === 'string' ? input.url : null
  const query = typeof input.query === 'string' ? input.query : null
  const header = url ?? (query != null ? `“${query}”` : null)

  const text = 'content' in payload ? extractText(payload.content) : null
  const cleaned = text != null ? stripAnsi(text) : ''

  return (
    <div>
      {header && (
        <div className="mb-1 truncate text-xs text-gray-500 dark:text-gray-400" title={header}>
          {name === 'WebSearch' ? 'search: ' : ''}{header}
        </div>
      )}
      {cleaned.trim() !== '' ? <Markdown>{cleaned}</Markdown> : <GenericResult payload={payload} ok={ok} />}
    </div>
  )
}
