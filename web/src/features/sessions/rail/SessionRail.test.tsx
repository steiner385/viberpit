import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { EMPTY_BACKGROUND_WORK_VIEW } from '@/session/selectors'

vi.mock('@/api/hooks', () => ({
  useSessionDiff: () => ({ data: undefined }),
  useSessionMcp: () => ({ data: undefined }),
  useReconnectSessionMcp: () => ({ mutate: vi.fn(), isPending: false, isError: false, error: null }),
  usePermissionRules: () => ({ data: [] }),
  useMemoryFile: () => ({ data: undefined }),
  useRevokePermissionRule: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useStopSubAgent: () => ({ mutate: vi.fn(), isPending: false }),
}))

import { SessionRail } from './SessionRail'

function Wrapper({ children }: { children: ReactNode }) {
  return <MemoryRouter>{children}</MemoryRouter>
}

const props = {
  changes: { sessionId: 's1', busy: false, onOpenChanges: () => {} },
  context: {
    sessionId: 's1', account: 'work', contextPct: 60, quota: null, commands: [], mcpLive: null, memoryFiles: null, cwd: null, onCompact: () => {},
  },
  environment: {
    account: 'work', accounts: [{ name: 'work' }], accountPinned: false, onSelectAccount: () => {},
  },
  tasks: { sessionId: 's1', subAgentsSupported: true, subAgents: [], backgroundWork: EMPTY_BACKGROUND_WORK_VIEW },
}

describe('SessionRail', () => {
  it('renders the four cards in order: Changes, Context, Environment, Tasks', () => {
    render(<SessionRail {...props} />, { wrapper: Wrapper })
    const titles = screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent)
    expect(titles).toEqual(['Changes', 'Context', 'Environment', 'Tasks'])
    for (const name of ['Changes', 'Context', 'Environment', 'Tasks']) {
      expect(screen.getByRole('region', { name })).toBeInTheDocument()
    }
  })
})
