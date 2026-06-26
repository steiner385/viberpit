import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'
import { useAutoReload } from './useAutoReload'
import * as hooks from '@/api/hooks'
import * as updatePending from '@/hooks/useUpdatePending'
import * as wsClient from '@/ws/client'

function Harness({ updateAvailable }: { updateAvailable: boolean }) {
  useAutoReload(updateAvailable)
  return null
}

function renderWith(updateAvailable: boolean) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <Harness updateAvailable={updateAvailable} />
    </QueryClientProvider>,
  )
}

const reload = vi.fn()
beforeEach(() => {
  reload.mockReset()
  try { sessionStorage.clear() } catch { /* no-op */ }
  vi.stubGlobal('location', { reload })
  vi.spyOn(wsClient, 'getSessionSocket').mockReturnValue(
    { activeSessionId: 's1', updatePendingState: { active: false, count: 0 } } as unknown as ReturnType<typeof wsClient.getSessionSocket>,
  )
})
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('useAutoReload', () => {
  it('reloads when update available + focused session idle + nothing buffered', () => {
    vi.spyOn(hooks, 'useSessions').mockReturnValue({ data: [{ id: 's1', status: 'idle' }] } as ReturnType<typeof hooks.useSessions>)
    vi.spyOn(updatePending, 'useUpdatePending').mockReturnValue({ active: false, count: 0 })
    renderWith(true)
    expect(reload).toHaveBeenCalledOnce()
  })

  it('does NOT reload while the focused session is mid-turn', () => {
    vi.spyOn(hooks, 'useSessions').mockReturnValue({ data: [{ id: 's1', status: 'in_progress' }] } as ReturnType<typeof hooks.useSessions>)
    vi.spyOn(updatePending, 'useUpdatePending').mockReturnValue({ active: false, count: 0 })
    renderWith(true)
    expect(reload).not.toHaveBeenCalled()
  })

  it('does NOT reload while prompts are buffered', () => {
    vi.spyOn(hooks, 'useSessions').mockReturnValue({ data: [{ id: 's1', status: 'idle' }] } as ReturnType<typeof hooks.useSessions>)
    vi.spyOn(updatePending, 'useUpdatePending').mockReturnValue({ active: true, count: 2 })
    renderWith(true)
    expect(reload).not.toHaveBeenCalled()
  })

  it('does NOT reload when no update is available', () => {
    vi.spyOn(hooks, 'useSessions').mockReturnValue({ data: [{ id: 's1', status: 'idle' }] } as ReturnType<typeof hooks.useSessions>)
    vi.spyOn(updatePending, 'useUpdatePending').mockReturnValue({ active: false, count: 0 })
    renderWith(false)
    expect(reload).not.toHaveBeenCalled()
  })

  it('reloads at most once per bundle — a still-outdated reload does NOT loop', () => {
    // Regression: a deploy left the edge reporting a sha the served bundle never matches,
    // so updateAvailable stayed true across reloads and useAutoReload reloaded forever.
    vi.spyOn(hooks, 'useSessions').mockReturnValue({ data: [{ id: 's1', status: 'idle' }] } as ReturnType<typeof hooks.useSessions>)
    vi.spyOn(updatePending, 'useUpdatePending').mockReturnValue({ active: false, count: 0 })

    // First load: still outdated + safe → reloads once, records the marker.
    renderWith(true).unmount()
    expect(reload).toHaveBeenCalledOnce()

    // The page comes back on the SAME bundle, still outdated. It must NOT reload again.
    reload.mockReset()
    renderWith(true)
    expect(reload).not.toHaveBeenCalled()
  })
})
