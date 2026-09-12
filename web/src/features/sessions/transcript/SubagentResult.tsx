import { stripAnsi, extractText } from '@/lib/toolContent'
import { Markdown } from './Markdown'
import { GenericResult } from './GenericResult'

interface Props {
  payload: Record<string, unknown>
  ok?: boolean
}

/** Task (subagent) result: the subagent's final message, rendered as Markdown
 * the same way assistant prose is — it is prose, not a data blob. */
export function SubagentResult({ payload, ok = true }: Props) {
  const text = 'content' in payload ? extractText(payload.content) : null
  const cleaned = text != null ? stripAnsi(text) : ''
  if (cleaned.trim() === '') return <GenericResult payload={payload} ok={ok} />
  return <Markdown>{cleaned}</Markdown>
}
