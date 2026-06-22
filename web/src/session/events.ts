// Server → client session events (the backend's normalized event wire shapes).
export type ToolKind = 'edit' | 'write' | 'read' | 'bash' | 'todo' | 'search' | 'unknown'

export interface EvAttachmentRef { id: string; mediaType: string; filename: string }
export interface EvUser          { type: 'user';          turnId: string; text: string; attachments?: EvAttachmentRef[]; srcUuid?: string | null }
export interface EvTurnStart     { type: 'turnStart';     turnId: string; srcUuid?: string | null }
export interface EvTurnEnd       { type: 'turnEnd';       turnId: string; usage?: Record<string, unknown> | null; costUsd?: number | null; srcUuid?: string | null }
export interface EvTextDelta     { type: 'textDelta';     turnId: string; text: string; srcUuid?: string | null }
export interface EvThinkingDelta { type: 'thinkingDelta'; turnId: string; text: string; srcUuid?: string | null }
export interface EvToolCall      { type: 'toolCall';      turnId: string; toolId: string; name: string; toolKind: ToolKind; input: Record<string, unknown>; srcUuid?: string | null }
export interface EvToolResult    { type: 'toolResult';    toolId: string; ok: boolean; payload: Record<string, unknown>; srcUuid?: string | null }
export interface EvPermissionRequest { type: 'permissionRequest'; permId: string; turnId: string; tool: string; toolKind: ToolKind; input: Record<string, unknown>; plan: boolean }
export interface EvQuestion      { type: 'question';      questionId: string; turnId: string; header: string; question: string; options: Array<Record<string, unknown>>; multiSelect: boolean }
export interface EvSessionMeta   { type: 'sessionMeta';   model: string; sessionId: string }
export interface EvTitle         { type: 'title';         title: string; source: string }
export interface EvStatus        { type: 'status';        state: string; count: number }
export interface EvReplayComplete { type: 'replayComplete' }
export interface EvError         { type: 'error';         code: string; message: string; resetsAt?: string | null }
export interface EvQueueUpdate   { type: 'queueUpdate';   items: string[] }

export type SessionEvent =
  | EvUser | EvTurnStart | EvTurnEnd | EvTextDelta | EvThinkingDelta
  | EvToolCall | EvToolResult | EvPermissionRequest | EvQuestion
  | EvSessionMeta | EvTitle | EvStatus | EvReplayComplete | EvError | EvQueueUpdate

// The set of `type` strings that are session events (used by the WS fan-out).
export const SESSION_EVENT_TYPES: ReadonlySet<string> = new Set([
  'user','turnStart','turnEnd','textDelta','thinkingDelta','toolCall','toolResult',
  'permissionRequest','question','sessionMeta','title','status','replayComplete','error','queueUpdate',
])
