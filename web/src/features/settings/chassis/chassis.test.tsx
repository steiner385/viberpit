import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { axe, toHaveNoViolations } from 'jest-axe'
import { SettingsSection } from './SettingsSection'
import { SettingsGroup } from './SettingsGroup'
import { SettingRow } from './SettingRow'
import { ScopeBadge } from './ScopeBadge'
import { Input } from '@/ui/Input'
import { Checkbox } from '@/ui/Checkbox'
import { RadioGroup } from '@/ui/RadioGroup'

expect.extend(toHaveNoViolations)

describe('settings chassis', () => {
  it('section is a region named by its h2, with description and scope badge', async () => {
    const { container } = render(
      <SettingsSection id="general" title="General" description="Paths and limits." scope="server">
        <p>body</p>
      </SettingsSection>,
    )
    const region = screen.getByRole('region', { name: 'General' })
    expect(region).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'General' })).toHaveAttribute('id', 'settings-general-title')
    expect(screen.getByText('Paths and limits.')).toBeInTheDocument()
    expect(screen.getByText('Server')).toBeInTheDocument()
    expect(await axe(container)).toHaveNoViolations()
  })

  it('group is a fieldset with a legend; danger tone marks it', () => {
    render(
      <SettingsGroup legend="Danger zone" tone="danger" description="Cannot be undone.">
        <span>x</span>
      </SettingsGroup>,
    )
    const grp = screen.getByRole('group', { name: 'Danger zone' })
    expect(grp).toHaveAttribute('data-tone', 'danger')
    expect(screen.getByText('Cannot be undone.')).toBeInTheDocument()
  })

  it('row wires label, description and error to the control via aria-describedby', () => {
    render(
      <SettingRow label="Repo clone root" htmlFor="repo-root" description="Where repos are cloned." error="Must be absolute.">
        <Input id="repo-root" />
      </SettingRow>,
    )
    const input = screen.getByLabelText('Repo clone root')
    expect(input).toHaveAttribute('aria-describedby', 'repo-root-desc repo-root-error')
    expect(screen.getByRole('alert')).toHaveTextContent('Must be absolute.')
  })

  it('selfLabelled row leaves the control ONE accessible name', async () => {
    // A `ui/Checkbox` wraps its input in its own <label>. Without the flag the
    // row adds a second <label htmlFor> and the two concatenate — the control is
    // then named "Auto-archive Auto-archive completed sessions" and axe reports
    // form-field-multiple-labels.
    const { container } = render(
      <SettingRow label="Auto-archive" htmlFor="auto-archive" description="Archived when the goal is met." selfLabelled>
        <Checkbox id="auto-archive" label="Auto-archive completed sessions" checked onChange={() => {}} />
      </SettingRow>,
    )
    const box = screen.getByRole('checkbox', { name: 'Auto-archive completed sessions' })
    expect(box).toHaveAttribute('aria-describedby', 'auto-archive-desc')
    expect(screen.getByText('Auto-archive')).not.toHaveAttribute('for')
    expect(await axe(container)).toHaveNoViolations()
  })

  it('row with a group control has no row label and no dangling htmlFor (R14b)', async () => {
    // A RadioGroup is named by its own legend and `htmlFor` cannot name a group,
    // so the row emits no <label>: one would point at an id no element carries
    // and focus nothing when clicked. The legend is the row's visible label.
    const { container } = render(
      <SettingRow htmlFor="tool-detail" description="How groups open." selfLabelled>
        <RadioGroup
          name="tool-detail"
          legend="Tool calls"
          value="auto"
          onChange={() => {}}
          options={[{ value: 'auto', label: 'Auto' }, { value: 'all-open', label: 'Expand all' }]}
        />
      </SettingRow>,
    )
    expect(container.querySelector('label[for="tool-detail"]')).toBeNull()
    expect(screen.getByRole('group', { name: 'Tool calls' })).toHaveAttribute('aria-describedby', 'tool-detail-desc')
    expect(await axe(container)).toHaveNoViolations()
  })

  it('row status is a polite live region', () => {
    render(
      <SettingRow label="Theme" htmlFor="theme" status="Saved">
        <Input id="theme" />
      </SettingRow>,
    )
    expect(screen.getByRole('status')).toHaveTextContent('Saved')
  })

  it('row keeps the status live region mounted when there is no status (R7)', () => {
    render(
      <SettingRow label="Theme" htmlFor="theme">
        <Input id="theme" />
      </SettingRow>,
    )
    const live = screen.getByRole('status')
    expect(live).toBeEmptyDOMElement()
    expect(live).toHaveAttribute('aria-live', 'polite')
  })

  it('ScopeBadge renders nothing for none', () => {
    const { container } = render(<ScopeBadge scope="none" />)
    expect(container).toBeEmptyDOMElement()
  })
})
