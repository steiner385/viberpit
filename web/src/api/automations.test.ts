// @vitest-environment node
import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  fetchAutomations, patchAutomation, fetchAutomationActionable, postAutomationTrigger,
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

  it('fetchAutomationActionable GETs the actionable list', async () => {
    const data = [{ id: 'o/r#7', title: 't', subtitle: null, url: 'https://x/7',
      actions: [{ kind: 'failing_ci', label: 'Fix CI', style: 'default' }] }]
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const out = await fetchAutomationActionable('pr-doctor')
    expect(out[0].id).toBe('o/r#7')
    expect(globalThis.fetch).toHaveBeenCalledWith('/api/automations/pr-doctor/actionable', expect.anything())
  })

  it('postAutomationTrigger POSTs the trigger body', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'sent', level: 'ok', message: 'ok' }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const out = await postAutomationTrigger('pr-doctor', { item_id: 'o/r#7', kind: 'failing_ci' })
    expect(out.status).toBe('sent')
    const [url, init] = (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(url).toBe('/api/automations/pr-doctor/trigger')
    expect(init.method).toBe('POST')
  })
})
