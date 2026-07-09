// Server → client session events (the backend's normalized event wire shapes).
export type ToolKind = 'edit' | 'write' | 'read' | 'bash' | 'todo' | 'search' | 'web' | 'task' | 'mcp' | 'unknown'

// One attached image on a user turn. `url` is ready to use directly as an <img> src:
// `/api/attachments/{id}` for a live turn, or a `data:` URL reconstructed on replay.
export interface EvUserImage     { url: string; mime: string; name: string }
export interface EvUser          { type: 'user';          turnId: string; text: string; images?: EvUserImage[]; srcUuid?: string | null; ts?: number }
export interface EvTurnStart     { type: 'turnStart';     turnId: string; srcUuid?: string | null; parentToolUseId?: string | null }
export interface EvTurnEnd       { type: 'turnEnd';       turnId: string; usage?: Record<string, unknown> | null; costUsd?: number | null; srcUuid?: string | null; parentToolUseId?: string | null }
export interface EvTextDelta     { type: 'textDelta';     turnId: string; text: string; srcUuid?: string | null; parentToolUseId?: string | null; ts?: number }
export interface EvThinkingDelta { type: 'thinkingDelta'; turnId: string; text: string; srcUuid?: string | null; parentToolUseId?: string | null; ts?: number }
export interface EvToolCall      { type: 'toolCall';      turnId: string; toolId: string; name: string; toolKind: ToolKind; input: Record<string, unknown>; srcUuid?: string | null; parentToolUseId?: string | null; ts?: number }
export interface EvToolResult    { type: 'toolResult';    toolId: string; ok: boolean; payload: Record<string, unknown>; srcUuid?: string | null; parentToolUseId?: string | null; ts?: number }
export interface EvPermissionRequest { type: 'permissionRequest'; permId: string; turnId: string; tool: string; toolKind: ToolKind; input: Record<string, unknown>; plan: boolean; ts?: number }
// A permission's future settled server-side (allowed or denied). Rides the same stream
// as the request frame so a mid-turn reattach (ring resupply re-delivers the request)
// clears the recreated card instead of resurrecting an already-decided permission.
export interface EvPermissionResolved { type: 'permissionResolved'; permId: string; turnId: string; allowed: boolean; ts?: number }
export interface EvQuestion      { type: 'question';      questionId: string; turnId: string; header: string; question: string; options: Array<Record<string, unknown>>; multiSelect: boolean; ts?: number }
// A question's future settled server-side — answered (selected carries the labels) or
// dismissed (selected null). Rides the same stream as the question frame so a mid-turn
// reattach (ring resupply re-delivers the question) re-resolves the recreated block.
export interface EvQuestionResolved { type: 'questionResolved'; questionId: string; turnId: string; selected: string[] | null; mode?: string; ts?: number }
export interface EvSessionMeta   { type: 'sessionMeta';   model: string; sessionId: string }
export interface EvTurnModel     { type: 'turnModel';     turnId: string; model: string; parentToolUseId?: string | null }
export interface EvTitle         { type: 'title';         title: string; source: string }
export interface EvStatus        { type: 'status';        state: string; count: number }
export interface EvReplayReset   { type: 'replayReset'; sessionId?: string }
export interface EvReplayComplete { type: 'replayComplete'; sessionId?: string; earliestTurn?: number; totalTurns?: number }
export interface EvError         { type: 'error';         code: string; message: string; resetsAt?: string | null; turnId?: string | null; ts?: number }
export interface QueuedItem      { text: string; images: { url: string; mime: string; name: string }[] }
export interface EvQueueUpdate   { type: 'queueUpdate';   items: QueuedItem[] }
export interface EvSuggestions   { type: 'suggestions';   sessionId: string; turnId: string; items: string[] }
export interface EvCondensing    { type: 'condensing';    active: boolean; expectedMs?: number | null }
// Exact context-window occupancy pulled from the SDK after a turn settles (same data
// as the `/context` command). Drives the persistent ctx% readout — see contextWindow.ts.
export interface EvContextUsage  { type: 'contextUsage';  usedTokens: number; maxTokens: number; percentage: number; model?: string | null }
export interface EvRecovering    { type: 'recovering';    turnId: string; message: string; ts?: number }

export type SessionEvent =
  | EvUser | EvTurnStart | EvTurnEnd | EvTextDelta | EvThinkingDelta
  | EvToolCall | EvToolResult | EvPermissionRequest | EvPermissionResolved | EvQuestion | EvQuestionResolved
  | EvSessionMeta | EvTurnModel | EvTitle | EvStatus | EvReplayReset | EvReplayComplete | EvError | EvQueueUpdate
  | EvSuggestions | EvCondensing | EvContextUsage | EvRecovering

export interface Command {
  name: string
  description: string
  argumentHint: string | null
  kind: 'client' | 'control' | 'passthrough'
}

// The set of `type` strings that are session events (used by the WS fan-out).
export const SESSION_EVENT_TYPES: ReadonlySet<string> = new Set([
  'user','turnStart','turnEnd','textDelta','thinkingDelta','toolCall','toolResult',
  'permissionRequest','permissionResolved','question','questionResolved','sessionMeta','turnModel','title','status','replayReset','replayComplete','error','queueUpdate',
  'suggestions','condensing','contextUsage','recovering',
])
