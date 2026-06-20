import type { ToolKind } from '@/session/events'
import { DiffView } from './DiffView'
import { FileContent } from './FileContent'
import { BashResult } from './BashResult'
import { TodoList } from './TodoList'

interface Props {
  kind: ToolKind
  ok: boolean
  payload: Record<string, unknown>
}

export function ToolResult({ kind, ok, payload }: Props) {
  switch (kind) {
    case 'edit':
    case 'write':
      return <DiffView payload={payload} />
    case 'read':
      return <FileContent payload={payload} />
    case 'bash':
      return <BashResult payload={payload} ok={ok} />
    case 'todo':
      return <TodoList payload={payload} />
    default:
      return (
        <pre className="overflow-auto rounded bg-gray-50 dark:bg-gray-900 p-2 text-xs font-mono text-gray-800 dark:text-gray-200">
          {JSON.stringify(payload, null, 2)}
        </pre>
      )
  }
}
