import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ManagedEnvTable, showManagedValue } from './ManagedEnvTable'

const FLAGS = [
  { key: 'CCSWITCH_ENABLE_COPILOT', value: false, description: 'Enable GitHub Copilot accounts' },
  { key: 'CCSWITCH_TEST_SLOT_CAP', value: 1, description: 'Full test-suite runs one host may carry at once' },
  { key: 'CCSWITCH_CHILD_SESSION_CAP', value: 3, description: 'Live child sessions one parent may hold' },
]

describe('ManagedEnvTable', () => {
  it('renders every flag when no key filter is given', () => {
    render(<ManagedEnvTable flags={FLAGS} />)
    expect(screen.getAllByRole('row')).toHaveLength(4) // header + 3
    expect(screen.getByRole('rowheader', { name: 'CCSWITCH_ENABLE_COPILOT' })).toBeInTheDocument()
  })

  it('filters to the given keys, in the given order', () => {
    render(<ManagedEnvTable flags={FLAGS} keys={['CCSWITCH_CHILD_SESSION_CAP', 'CCSWITCH_TEST_SLOT_CAP', 'NOT_SENT']} />)
    const names = screen.getAllByRole('rowheader').map((th) => th.textContent)
    expect(names).toEqual(['CCSWITCH_CHILD_SESSION_CAP', 'CCSWITCH_TEST_SLOT_CAP'])
  })

  it('shows booleans as on/off and null as unset', () => {
    expect(showManagedValue(true)).toBe('on')
    expect(showManagedValue(false)).toBe('off')
    expect(showManagedValue(null)).toBe('unset')
    expect(showManagedValue(3)).toBe('3')
  })
})
