import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { SESSION_ROUTE, SUBAGENT_ROUTE, ARCHIVED_SESSION_ROUTE, ARCHIVED_PATH, SESSIONS_PATH } from '@/session/sessionNavState'
import { useOpenItem, useOpenSessionId } from './openSessionId'

function Probe() {
  const item = useOpenItem()
  const sessionId = useOpenSessionId()
  return <div data-testid="probe">{`${item ? `${item.kind}:${item.id}` : 'none'}|${sessionId ?? '-'}`}</div>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        {[SESSIONS_PATH, SESSION_ROUTE, SUBAGENT_ROUTE, ARCHIVED_PATH, ARCHIVED_SESSION_ROUTE, '/pipeline'].map((p) => (
          <Route key={p} path={p} element={<Probe />} />
        ))}
      </Routes>
    </MemoryRouter>,
  )
}

describe('useOpenItem', () => {
  it.each([
    ['/sessions', 'none|-'],
    ['/sessions/abc', 'session:abc|abc'],
    ['/sessions/abc/agents/x', 'session:abc|abc'],
    ['/sessions/archived', 'none|-'],
    ['/pipeline', 'none|-'],
  ])('%s → %s', (path, expected) => {
    renderAt(path)
    expect(screen.getByTestId('probe')).toHaveTextContent(expected)
  })

  it('reports an archived transcript as open, but never as an open SESSION', () => {
    // Both halves matter: the shell must know something is open (so a phone
    // shows the reader instead of the list), and PageHost must not attach a
    // live runtime to it.
    renderAt('/sessions/archived/abc')
    expect(screen.getByTestId('probe')).toHaveTextContent('archived:abc|-')
  })
})
