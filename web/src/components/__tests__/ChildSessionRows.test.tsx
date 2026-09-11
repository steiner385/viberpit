import { render, screen, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'

const navigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const real = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...real, useNavigate: () => navigate }
})

import { ChildSessionRows } from '../ChildSessionRows'
import type { Session } from '@/api/types'

const child = { id: 'c1', name: 'API redesign', status: 'waiting_question', accountName: 'a', engine: 'claude_code' } as unknown as Session

describe('ChildSessionRows', () => {
  it('renders one row per child with its status and navigates to the child', () => {
    render(<MemoryRouter><ChildSessionRows children={[child]} /></MemoryRouter>)
    const row = screen.getByRole('button', { name: /Open child session API redesign/ })
    expect(row).toHaveTextContent('API redesign')
    expect(row).toHaveTextContent('waiting_question')
    fireEvent.click(row)
    expect(navigate).toHaveBeenCalledWith(expect.stringContaining('/sessions/c1'))
  })
})
