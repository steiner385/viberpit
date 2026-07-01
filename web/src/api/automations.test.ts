import { describe, it, expect, vi, afterEach } from 'vitest'
import { fetchAutomations, patchAutomation } from './automations'

afterEach(() => vi.restoreAllMocks())

describe('automations api', () => {
  it('fetchAutomations GETs /api/automations', async () => {
    const data = [{ id: 'pr-doctor', name: 'PR Doctor', modes: ['off', 'auto'] }]
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const out = await fetchAutomations()
    expect(out[0].id).toBe('pr-doctor')
    expect(globalThis.fetch).toHaveBeenCalledWith('/api/automations', expect.anything())
  })

  it('patchAutomation PATCHes the body', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ id: 'pr-doctor', enabled: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const out = await patchAutomation('pr-doctor', { enabled: true })
    expect(out.enabled).toBe(true)
    const [, init] = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(init.method).toBe('PATCH')
  })
})
