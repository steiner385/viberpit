import { cn } from '@/lib/utils'
import { stripAnsi, extractText } from '@/lib/toolContent'
import { GenericResult } from './GenericResult'

interface Props {
  payload: Record<string, unknown>
  ok?: boolean
  /** Tool name (Grep / Glob / LS) — picks the "matches" vs "files" wording. */
  name?: string
}

function noun(name: string | undefined): [string, string] {
  if (name === 'Grep') return ['match', 'matches']
  if (name === 'Glob' || name === 'LS') return ['file', 'files']
  return ['result', 'results']
}

/** Grep/Glob/LS results: the matched lines plus a count summary. */
export function SearchResult({ payload, ok = true, name }: Props) {
  const text = 'content' in payload ? extractText(payload.content) : null
  // Structured search payloads (rare) fall through to the generic renderer.
  if (text == null) return <GenericResult payload={payload} ok={ok} />

  const cleaned = stripAnsi(text).replace(/\n+$/, '')
  const lines = cleaned === '' ? [] : cleaned.split('\n')
  const [singular, plural] = noun(name)
  const summary = lines.length === 0 ? `no ${plural}` : `${lines.length} ${lines.length === 1 ? singular : plural}`

  return (
    <div>
      <div className="mb-1 text-xs text-gray-500 dark:text-gray-400">{summary}</div>
      {lines.length > 0 && (
        <pre
          className={cn(
            'max-h-64 overflow-auto rounded p-2 text-xs font-mono md:max-h-96',
            ok
              ? 'bg-gray-50 dark:bg-gray-900 text-gray-800 dark:text-gray-200'
              : 'bg-red-50 dark:bg-red-950/30 text-red-800 dark:text-red-300',
          )}
        >
          {lines.join('\n')}
        </pre>
      )}
    </div>
  )
}
