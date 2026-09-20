// `ConfigInput` — the shared automation/policy field renderer. Covered here:
// an int field renders the schema's `min` on the number input, so the browser
// offers exactly the range the server enforces (`ConfigField.min` → 422).
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ConfigField } from '@/api/automations'
import { ConfigInput } from './ConfigInput'

vi.mock('@/api/hooks', () => ({
  useGithubRepos: () => ({ data: [], isLoading: false }),
}))

function intField(over: Partial<ConfigField> = {}): ConfigField {
  return { key: 'cap', label: 'Max in flight', type: 'int', default: 1, help: '',
           choices: null, ...over }
}

function stringListField(over: Partial<ConfigField> = {}): ConfigField {
  return { key: 'skip_labels', label: 'Labels to skip', type: 'string-list', default: [],
           help: '', choices: null, ...over }
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

  it('renders string-list values as removable chips', () => {
    const onChange = vi.fn()
    render(<ConfigInput field={stringListField()} value={['on-hold', 'renovate']} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Remove on-hold' }))
    expect(onChange).toHaveBeenCalledWith('skip_labels', ['renovate'])
  })

  it('commits a typed label as a new chip on Enter and clears the draft', () => {
    const onChange = vi.fn()
    render(<ConfigInput field={stringListField()} value={['renovate']} onChange={onChange} />)
    const textbox = screen.getByRole('textbox')
    fireEvent.change(textbox, { target: { value: 'on-hold' } })
    fireEvent.keyDown(textbox, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('skip_labels', ['renovate', 'on-hold'])
    expect((textbox as HTMLInputElement).value).toBe('')
  })

  it('ignores a duplicate label, case-insensitively', () => {
    const onChange = vi.fn()
    render(<ConfigInput field={stringListField()} value={['on-hold']} onChange={onChange} />)
    const textbox = screen.getByRole('textbox')
    fireEvent.change(textbox, { target: { value: 'On-Hold' } })
    fireEvent.keyDown(textbox, { key: 'Enter' })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('pops the last chip on Backspace when the draft is empty', () => {
    const onChange = vi.fn()
    render(<ConfigInput field={stringListField()} value={['renovate', 'on-hold']} onChange={onChange} />)
    const textbox = screen.getByRole('textbox')
    fireEvent.keyDown(textbox, { key: 'Backspace' })
    expect(onChange).toHaveBeenCalledWith('skip_labels', ['renovate'])
  })
})
