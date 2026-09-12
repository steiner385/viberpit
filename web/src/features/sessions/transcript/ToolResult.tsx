import type { ToolKind } from '@/session/events'
import { DiffView } from './DiffView'
import { FileContent } from './FileContent'
import { BashResult } from './BashResult'
import { TodoList } from './TodoList'
import { SearchResult } from './SearchResult'
import { SubagentResult } from './SubagentResult'
import { WebResult } from './WebResult'
import { GenericResult } from './GenericResult'

interface Props {
  kind: ToolKind
  ok: boolean
  /** The tool's call arguments — Edit/Write diffs are reconstructed from here. */
  input: Record<string, unknown>
  payload: Record<string, unknown>
  /** Tool name (e.g. Grep, WebFetch, mcp__github__list_prs) for renderer context. */
  name?: string
}

export function ToolResult({ kind, ok, input, payload, name }: Props) {
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
    case 'search':
      return <SearchResult payload={payload} ok={ok} name={name} />
    case 'task':
      return <SubagentResult payload={payload} ok={ok} />
    case 'web':
      return <WebResult input={input} payload={payload} ok={ok} name={name} />
    case 'mcp':
    default:
      return <GenericResult payload={payload} ok={ok} />
  }
}
