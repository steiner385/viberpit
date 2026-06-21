import { cn } from '@/lib/utils'

interface Props { payload: Record<string, unknown>; ok?: boolean }

function stripAnsi(s: string): string {
  // ANSI escape sequences start with ESC (0x1b); matching them legitimately
  // requires the control character.
  // eslint-disable-next-line no-control-regex
  return s.replace(/\x1b\[[0-9;]*m/g, '')
}

function extractContent(payload: Record<string, unknown>): string {
  if (typeof payload.content === 'string') return payload.content
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
