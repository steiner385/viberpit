// Server → client session events (the backend's normalized event wire shapes).
export type ToolKind = 'edit' | 'write' | 'read' | 'bash' | 'todo' | 'search' | 'web' | 'task' | 'mcp' | 'unknown'

// One attached image on a user turn. `url` is ready to use directly as an <img> src:
// `/api/attachments/{id}` for a live turn, or a `data:` URL reconstructed on replay.
export interface EvUserImage     { url: string; mime: string; name: string }
export interface EvUser          { type: 'user';          turnId: string; text: string; images?: EvUserImage[]; srcUuid?: string | null }
export interface EvTurnStart     { type: 'turnStart';     turnId: string; srcUuid?: string | null; parentToolUseId?: string | null }
export interface EvTurnEnd       { type: 'turnEnd';       turnId: string; usage?: Record<string, unknown> | null; costUsd?: number | null; srcUuid?: string | null; parentToolUseId?: string | null }
export interface EvTextDelta     { type: 'textDelta';     turnId: string; text: string; srcUuid?: string | null; parentToolUseId?: string | null }
export interface EvThinkingDelta { type: 'thinkingDelta'; turnId: string; text: string; srcUuid?: string | null; parentToolUseId?: string | null }
export interface EvToolCall      { type: 'toolCall';      turnId: string; toolId: string; name: string; toolKind: ToolKind; input: Record<string, unknown>; srcUuid?: string | null; parentToolUseId?: string | null }
export interface EvToolResult    { type: 'toolResult';    toolId: string; ok: boolean; payload: Record<string, unknown>; srcUuid?: string | null; parentToolUseId?: string | null }
export interface EvPermissionRequest { type: 'permissionRequest'; permId: string; turnId: string; tool: string; toolKind: ToolKind; input: Record<string, unknown>; plan: boolean }
export interface EvQuestion      { type: 'question';      questionId: string; turnId: string; header: string; question: string; options: Array<Record<string, unknown>>; multiSelect: boolean }
export interface EvSessionMeta   { type: 'sessionMeta';   model: string; sessionId: string; account?: string | null; accountPinned?: boolean }
export interface EvTitle         { type: 'title';         title: string; source: string }
export interface EvStatus        { type: 'status';        state: string; count: number }
export interface EvReplayReset   { type: 'replayReset' }
export interface EvReplayComplete { type: 'replayComplete' }
export interface EvError         { type: 'error';         code: string; message: string; resetsAt?: string | null; turnId?: string | null }
export interface EvQueueUpdate   { type: 'queueUpdate';   items: string[] }
export interface EvSuggestions   { type: 'suggestions';   sessionId: string; turnId: string; items: string[] }

export type SessionEvent =
  | EvUser | EvTurnStart | EvTurnEnd | EvTextDelta | EvThinkingDelta
  | EvToolCall | EvToolResult | EvPermissionRequest | EvQuestion
  | EvSessionMeta | EvTitle | EvStatus | EvReplayReset | EvReplayComplete | EvError | EvQueueUpdate
  | EvSuggestions

export interface Command {
  name: string
  description: string
  argumentHint: string | null
  kind: 'client' | 'control' | 'passthrough'
}

// The set of `type` strings that are session events (used by the WS fan-out).
export const SESSION_EVENT_TYPES: ReadonlySet<string> = new Set([
  'user','turnStart','turnEnd','textDelta','thinkingDelta','toolCall','toolResult',
  'permissionRequest','question','sessionMeta','title','status','replayReset','replayComplete','error','queueUpdate',
  'suggestions',
])
