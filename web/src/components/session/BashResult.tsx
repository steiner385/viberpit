import { cn } from '@/lib/utils'
import { stripAnsi, coerceText } from '@/lib/toolContent'

interface Props { payload: Record<string, unknown>; ok?: boolean }

function extractContent(payload: Record<string, unknown>): string {
  // `content` may be a string OR a list of content blocks — normalize both.
  if (payload.content != null) {
    const text = coerceText(payload.content)
    if (text) return text
  }
  const parts: string[] = []
  if (typeof payload.stdout === 'string' && payload.stdout) parts.push(payload.stdout)
  if (typeof payload.stderr === 'string' && payload.stderr) parts.push(payload.stderr)
  if (parts.length > 0) return parts.join('\n')
  return JSON.stringify(payload, null, 2)
}

export function BashResult({ payload, ok = true }: Props) {
  const content = stripAnsi(extractContent(payload))

  return (
    <pre
      className={cn(
        'max-h-64 overflow-auto rounded p-2 text-xs font-mono md:max-h-96',
        ok
          ? 'bg-gray-50 dark:bg-gray-900 text-gray-800 dark:text-gray-200'
          : 'bg-red-50 dark:bg-red-950/30 text-red-800 dark:text-red-300',
      )}
    >
      {content}
    </pre>
  )
}
