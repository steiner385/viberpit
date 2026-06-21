import type { ToolKind } from '@/session/events'
import { cn } from '@/lib/utils'
import { DiffView } from './DiffView'
import { FileContent } from './FileContent'
import { BashResult } from './BashResult'
import { TodoList } from './TodoList'

interface Props {
  kind: ToolKind
  ok: boolean
  /** The tool's call arguments — Edit/Write diffs are reconstructed from here. */
  input: Record<string, unknown>
  payload: Record<string, unknown>
}

export function ToolResult({ kind, ok, input, payload }: Props) {
  switch (kind) {
    case 'edit':
    case 'write':
      return <DiffView input={input} ok={ok} />
    case 'read':
      return <FileContent payload={payload} ok={ok} />
    case 'bash':
      return <BashResult payload={payload} ok={ok} />
    case 'todo':
      return <TodoList payload={payload} />
    default:
      return (
        <pre
          className={cn(
            'max-h-64 overflow-auto rounded p-2 text-xs font-mono md:max-h-96',
            ok
              ? 'bg-gray-50 dark:bg-gray-900 text-gray-800 dark:text-gray-200'
              : 'bg-red-50 dark:bg-red-950/30 text-red-800 dark:text-red-300',
          )}
        >
          {JSON.stringify(payload, null, 2)}
        </pre>
      )
  }
}
