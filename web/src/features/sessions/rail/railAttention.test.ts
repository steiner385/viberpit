import { describe, it, expect } from 'vitest'
import type { McpServerState } from '@/api/types'
import { EMPTY_BACKGROUND_WORK_VIEW, type BackgroundWorkRow, type SubAgent } from '@/session/selectors'
import { railNeedsAttention } from './railAttention'

const clear = { backgroundWork: EMPTY_BACKGROUND_WORK_VIEW, subAgents: [] as SubAgent[], mcpServers: [] as McpServerState[] }
const row = (status: BackgroundWorkRow['status']): BackgroundWorkRow =>
  ({ toolUseId: 't', kind: 'shell', description: 'npm test', status, live: true })
const agent = (status: SubAgent['status']): SubAgent => ({ toolId: 'a', type: 'Explore', description: 'look', status })
const mcp = (status: string): McpServerState => ({ name: 'gh', status, error: null, toolCount: null, scope: null })

describe('railNeedsAttention', () => {
  it('is false when all is clear', () => {
    expect(railNeedsAttention(clear)).toBe(false)
  })

  it('is false for finished background work, done agents and healthy servers', () => {
    expect(railNeedsAttention({
      backgroundWork: { rows: [row('completed')], wakeup: false, wakeupAt: null },
      subAgents: [agent('done'), agent('running'), agent('stopped')],
      mcpServers: [mcp('connected'), mcp('disabled'), mcp('pending')],
    })).toBe(false)
  })

  it('fires on running background work', () => {
    expect(railNeedsAttention({ ...clear, backgroundWork: { rows: [row('running')], wakeup: false, wakeupAt: null } })).toBe(true)
  })

  it('fires on an armed wakeup', () => {
    expect(railNeedsAttention({ ...clear, backgroundWork: { rows: [], wakeup: true, wakeupAt: null } })).toBe(true)
  })

  it('fires on a failed sub-agent', () => {
    expect(railNeedsAttention({ ...clear, subAgents: [agent('done'), agent('failed')] })).toBe(true)
  })

  it('fires on a failed MCP server', () => {
    expect(railNeedsAttention({ ...clear, mcpServers: [mcp('connected'), mcp('failed')] })).toBe(true)
  })

  it('fires on an MCP server that needs auth', () => {
    expect(railNeedsAttention({ ...clear, mcpServers: [mcp('needs-auth')] })).toBe(true)
  })
})
