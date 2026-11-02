import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ModelPicker } from './ModelPicker'

const MODELS = [{ id: 'opus', label: 'Opus 4' }, { id: 'sonnet', label: 'Sonnet 4' }]

function renderPicker(over: Partial<React.ComponentProps<typeof ModelPicker>> = {}) {
  const onSelect = vi.fn()
  render(<ModelPicker chosen="opus" live={null} pending={false} models={MODELS} onSelect={onSelect} {...over} />)
  return { onSelect }
}

const trigger = () => screen.getByRole('button', { name: /^Model:/ })

async function pick(name: RegExp) {
  await userEvent.click(trigger())
  await userEvent.click(screen.getByRole('menuitemradio', { name }))
}

describe('ModelPicker', () => {
  it('shows the chosen alias label, never the SDK id', () => {
    // The live turn still runs the old model: the trigger must not snap back to it.
    renderPicker({ chosen: 'sonnet', live: 'claude-opus-4-8' })
    expect(trigger()).toHaveTextContent('Sonnet 4')
    expect(trigger()).not.toHaveTextContent(/claude-/)
    expect(screen.getByLabelText('Model in use')).toHaveTextContent('Opus 4.8')
  })

  it('renders a Codex live model through the shared label helper', () => {
    renderPicker({ chosen: 'gpt-5', live: 'gpt-5.1-codex', engine: 'codex', models: [{ id: 'gpt-5', label: 'GPT-5' }] })
    expect(screen.getByLabelText('Model in use')).toHaveTextContent('GPT-5.1 Codex')
  })

  it('reads a null choice as Default, even before the catalog loads', () => {
    renderPicker({ chosen: null, models: [] })
    expect(trigger()).toHaveTextContent('Default')
    expect(screen.queryByLabelText('Model in use')).toBeNull()
  })

  it('shows a pending switch as "after this turn"', () => {
    renderPicker({ chosen: 'sonnet', pending: true })
    expect(screen.getByLabelText('Model switch pending')).toHaveTextContent(/after this turn/i)
  })

  it('marks the chosen model checked', async () => {
    renderPicker()
    await userEvent.click(trigger())
    expect(screen.getByRole('menu', { name: 'Model' })).toBeInTheDocument()
    expect(screen.getByRole('menuitemradio', { name: /^Opus 4/ })).toHaveAttribute('aria-checked', 'true')
  })

  it('idle session: selecting a model switches now, no dialog', async () => {
    const { onSelect } = renderPicker()
    await pick(/^Sonnet 4/)
    expect(onSelect).toHaveBeenCalledWith('sonnet', 'now')
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('selecting the current model is a no-op, even while busy', async () => {
    const { onSelect } = renderPicker({ busy: true })
    await pick(/^Opus 4/)
    expect(onSelect).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  describe('busy session', () => {
    it('opens ModelSwitchDialog (labelled by model label) and does not switch yet', async () => {
      const { onSelect } = renderPicker({ busy: true })
      await pick(/^Sonnet 4/)
      expect(screen.getByRole('dialog', { name: 'Switch model?' })).toHaveTextContent('Sonnet 4')
      expect(onSelect).not.toHaveBeenCalled()
    })

    it('"Stop & switch now" switches now and closes the dialog', async () => {
      const { onSelect } = renderPicker({ busy: true })
      await pick(/^Sonnet 4/)
      await userEvent.click(screen.getByRole('button', { name: 'Stop & switch now' }))
      expect(onSelect).toHaveBeenCalledWith('sonnet', 'now')
      expect(screen.queryByRole('dialog')).toBeNull()
    })

    it('"Switch after this turn" defers the switch', async () => {
      const { onSelect } = renderPicker({ busy: true })
      await pick(/^Sonnet 4/)
      await userEvent.click(screen.getByRole('button', { name: 'Switch after this turn' }))
      expect(onSelect).toHaveBeenCalledWith('sonnet', 'after_turn')
      expect(screen.queryByRole('dialog')).toBeNull()
    })

    it('Cancel closes without switching; the trigger keeps the chosen model', async () => {
      const { onSelect } = renderPicker({ busy: true })
      await pick(/^Sonnet 4/)
      await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
      expect(onSelect).not.toHaveBeenCalled()
      expect(screen.queryByRole('dialog')).toBeNull()
      expect(trigger()).toHaveTextContent('Opus 4')
    })

    it('offers "switch without stopping" for an engine that can steer', async () => {
      const { onSelect } = renderPicker({ busy: true, canSteer: true })
      await pick(/^Sonnet 4/)
      await userEvent.click(screen.getByRole('button', { name: 'Switch without stopping' }))
      expect(onSelect).toHaveBeenCalledWith('sonnet', 'after_turn')
    })
  })

  it('is controllable from outside (the Alt+Shift+I shortcut)', () => {
    const onOpenChange = vi.fn()
    renderPicker({ open: true, onOpenChange })
    expect(screen.getByRole('menu', { name: 'Model' })).toBeInTheDocument()
  })
})
