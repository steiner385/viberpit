import { describe, it, expect, vi, afterEach } from 'vitest'
import { fetchOlderTurns, exportTranscriptUrl } from '../transcript'

afterEach(() => vi.restoreAllMocks())

describe('transcript api', () => {
  it('fetchOlderTurns requests the paged endpoint', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ events: [], earliestTurn: 0, totalTurns: 9 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const res = await fetchOlderTurns('sid', 10, 5)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/sessions/sid/transcript?before=10&limit=5', expect.anything())
    expect(res.totalTurns).toBe(9)
  })

  it('exportTranscriptUrl builds the right path', () => {
    expect(exportTranscriptUrl('sid', 'md')).toBe('/api/sessions/sid/export.md')
    expect(exportTranscriptUrl('sid', 'jsonl')).toBe('/api/sessions/sid/export.jsonl')
  })
})
