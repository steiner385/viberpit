import { api } from './client'
import type { SessionEvent } from '@/session/events'

export interface OlderTurns {
  events: SessionEvent[]
  earliestTurn: number
  totalTurns: number
}

export function fetchOlderTurns(sessionId: string, before: number, limit = 20): Promise<OlderTurns> {
  return api.get<OlderTurns>(`/sessions/${sessionId}/transcript?before=${before}&limit=${limit}`)
}

/** One transcript search hit (D-11, `GET /sessions/{id}/search`). `ordinal` is the
 *  turn's index in the `?before=` cursor space; `matchStart`/`matchEnd` are UTF-16
 *  offsets into `snippet`. A user hit's `turnId` is the prompt's id, an assistant
 *  hit's the turn's. */
export interface SearchHit {
  turnId: string
  ordinal: number
  role: 'user' | 'assistant'
  snippet: string
  matchStart: number
  matchEnd: number
}

export interface TranscriptSearchResult {
  hits: SearchHit[]
  /** More hits exist past the limit — the count shows as `M+`. */
  truncated: boolean
  totalTurns?: number
}

/** Search the session's transcript (every term, case-insensitive substring). */
export function fetchTranscriptSearch(sessionId: string, q: string): Promise<TranscriptSearchResult> {
  return api.get<TranscriptSearchResult>(`/sessions/${sessionId}/search?q=${encodeURIComponent(q)}`)
}

export function exportTranscriptUrl(sessionId: string, format: 'md' | 'jsonl'): string {
  return `/api/sessions/${sessionId}/export.${format}`
}

/** Trigger a browser download of the transcript export. */
export function downloadTranscript(sessionId: string, format: 'md' | 'jsonl'): void {
  const a = document.createElement('a')
  a.href = exportTranscriptUrl(sessionId, format)
  a.download = ''
  document.body.appendChild(a)
  a.click()
  a.remove()
}
