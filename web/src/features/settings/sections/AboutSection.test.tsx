import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import * as hooks from '@/api/hooks'
import { appSettings } from '@/test/factories'
import { queryResult } from '@/test/reactQuery'
import { AboutSection } from './AboutSection'

expect.extend(toHaveNoViolations)

vi.mock('@/api/hooks', () => ({ useSettings: vi.fn() }))

describe('AboutSection', () => {
  it('shows version, host and the managed env table', async () => {
    vi.mocked(hooks.useSettings).mockReturnValue(queryResult(appSettings({
      about: { gitSha: 'f92c6e34', hostName: 'dobby' },
      managed: [{ key: 'CCSWITCH_ENABLE_COPILOT', value: false, description: 'Enable GitHub Copilot accounts' }],
    })))
    const router = createMemoryRouter([{ path: '/', element: <AboutSection /> }])
    const { container } = render(<RouterProvider router={router} />)
    expect(screen.getByRole('heading', { level: 2, name: 'About' })).toBeInTheDocument()
    expect(screen.getByText('f92c6e34')).toBeInTheDocument()
    expect(screen.getByText('dobby')).toBeInTheDocument()
    const row = screen.getByRole('row', { name: /CCSWITCH_ENABLE_COPILOT/ })
    expect(row).toHaveTextContent('off')
    expect(row).toHaveTextContent('Enable GitHub Copilot accounts')
    expect(screen.getByText(/\/etc\/ccswitch\/ccswitch\.env/)).toBeInTheDocument()
    expect(await axe(container)).toHaveNoViolations()
  })
})
