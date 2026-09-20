import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { queryResult } from '@/test/reactQuery'
import { CAPACITY } from '@/features/delivery/capacity/__fixtures__/capacity'
import { HostedSection } from './HostedSection'
import { SETTINGS_BY_ID } from './settingsDefs'

vi.mock('@/api/hooks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/hooks')>()
  return { ...actual, useCapacity: vi.fn(() => queryResult(CAPACITY)) }
})

function mount(id: 'fleet' | 'presets') {
  const def = { ...SETTINGS_BY_ID[id], component: () => <p>body</p> }
  const router = createMemoryRouter([{ path: '/', element: <HostedSection def={def} /> }])
  return render(<RouterProvider router={router} />)
}

describe('HostedSection readout slot (spec 2026-09-20 D10)', () => {
  it('a def with `readout` gets the live readout card after its body', () => {
    mount('fleet')
    expect(screen.getByText('body')).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Live readout' })).toBeInTheDocument()
  })
  it('a def without `readout` does not', () => {
    mount('presets')
    expect(screen.queryByRole('group', { name: 'Live readout' })).toBeNull()
  })
})
