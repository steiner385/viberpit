// `ConfigInput` — the shared automation/policy field renderer. Covered here:
// an int field renders the schema's `min` on the number input, so the browser
// offers exactly the range the server enforces (`ConfigField.min` → 422).
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { ConfigField } from '@/api/automations'
import { ConfigInput } from './ConfigInput'

vi.mock('@/api/hooks', () => ({
  useGithubRepos: () => ({ data: [], isLoading: false }),
}))

function intField(over: Partial<ConfigField> = {}): ConfigField {
  return { key: 'cap', label: 'Max in flight', type: 'int', default: 1, help: '',
           choices: null, ...over }
}

describe('ConfigInput', () => {
  it('renders an int field with the schema minimum', () => {
    render(<ConfigInput field={intField({ min: 0 })} value={2} onChange={vi.fn()} />)
    expect(screen.getByRole('spinbutton')).toHaveAttribute('min', '0')
  })

  it('leaves the input unbounded when the field declares no minimum', () => {
    render(<ConfigInput field={intField({ min: null })} value={2} onChange={vi.fn()} />)
    expect(screen.getByRole('spinbutton')).not.toHaveAttribute('min')
  })
})
