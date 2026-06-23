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
