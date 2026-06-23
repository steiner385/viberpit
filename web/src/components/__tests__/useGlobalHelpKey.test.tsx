import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useGlobalHelpKey } from '../useGlobalHelpKey'

function Harness({ onShowHelp }: { onShowHelp: () => void }) {
  useGlobalHelpKey({ enabled: true, onShowHelp })
  return <input aria-label="field" />
}

describe('useGlobalHelpKey', () => {
  it('opens help on ? when not in a field', async () => {
    const onShowHelp = vi.fn()
    const user = userEvent.setup()
    render(<Harness onShowHelp={onShowHelp} />)
    await user.keyboard('{Shift>}/{/Shift}')
    expect(onShowHelp).toHaveBeenCalledTimes(1)
  })
  it('does NOT open help when typing ? into an input', async () => {
    const onShowHelp = vi.fn()
    const user = userEvent.setup()
    const { getByLabelText } = render(<Harness onShowHelp={onShowHelp} />)
    await user.click(getByLabelText('field'))
    await user.keyboard('{Shift>}/{/Shift}')
    expect(onShowHelp).not.toHaveBeenCalled()
  })
})
