/**
 * A failed live tool toggle re-reads the session MCP GET (D-16b final review): a refused or
 * timed-out write may have changed `toolsEnforced`, so the panel must not keep the stale
 * snapshot. Real React Query hooks; only the HTTP layer is stubbed.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api, ApiRequestError } from '@/api/client'
import type { McpServerState, McpTool } from '@/api/types'
import { McpServerList } from './McpServerList'

const tool = (name: string): McpTool => ({
  name, description: null, enabled: true, source: 'default',
  annotations: { readOnly: null, destructive: null, openWorld: null },
})

const server = (toolsEnforced: boolean): McpServerState => ({
  name: 'probe', status: 'connected', error: null, toolCount: 1, scope: 'user',
  tools: [tool('tool_a')], toolsEnforced,
})

afterEach(() => vi.restoreAllMocks())

describe('McpServerList tool switches after a failed toggle', () => {
  it('refetches the session MCP GET and then shows the not-applied notice', async () => {
    const get = vi.spyOn(api, 'get')
      .mockResolvedValueOnce({ servers: [server(true)] })
      .mockResolvedValue({ servers: [server(false)] })
    vi.spyOn(api, 'post').mockRejectedValue(
      new ApiRequestError(409, { detail: { code: 'SETTINGS_FAILED', message: 'The CLI did not answer the settings write.' } }),
    )
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <McpServerList sessionId="s1" account="work" servers={[server(true)]} toolSwitches />
      </QueryClientProvider>,
    )
    await userEvent.click(screen.getByRole('button', { name: 'Tools for probe' }))
    const sw = await screen.findByRole('switch', { name: 'tool_a enabled' })
    expect(screen.queryByText(/Tool settings not applied/)).toBeNull()
    const reads = get.mock.calls.length

    await userEvent.click(sw)
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(reads))
    expect(get).toHaveBeenLastCalledWith('/sessions/s1/mcp')
    expect(await screen.findByText('Tool settings not applied — restart the session')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('The CLI did not answer the settings write.')
  })
})
