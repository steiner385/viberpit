import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  fetchAutomations, patchAutomation, fetchPrDoctorActionable, postPrDoctorFix,
} from './automations'

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

  it('fetchPrDoctorActionable GETs the actionable list', async () => {
    const data = [{ repo: 'o/r', number: 7, kind: 'failing_ci', url: 'https://x/7', title: 't' }]
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const out = await fetchPrDoctorActionable()
    expect(out[0].number).toBe(7)
    expect(globalThis.fetch).toHaveBeenCalledWith('/api/automations/pr-doctor/actionable', expect.anything())
  })

  it('postPrDoctorFix POSTs the fix body', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'sent' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const out = await postPrDoctorFix({ repo: 'o/r', number: 7, kind: 'failing_ci' })
    expect(out.status).toBe('sent')
    const [url, init] = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(url).toBe('/api/automations/pr-doctor/fix')
    expect(init.method).toBe('POST')
  })
})
