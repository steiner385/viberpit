/**
 * Unit tests for HelperKeyBar component.
 *
 * The component accepts an `onKey` prop so it's testable without a real WebSocket.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HelperKeyBar } from '../HelperKeyBar'

describe('HelperKeyBar', () => {
  let onKey: ReturnType<typeof vi.fn>

  beforeEach(() => {
    onKey = vi.fn()
  })

  async function setup() {
    const user = userEvent.setup()
    render(<HelperKeyBar onKey={onKey} />)
    return { user }
  }

  it('calls onKey with \\x1b when Esc is clicked', async () => {
    const { user } = await setup()
    await user.click(screen.getByRole('button', { name: /escape/i }))
    expect(onKey).toHaveBeenCalledWith('\x1b')
  })

  it('calls onKey with \\t when Tab is clicked', async () => {
    const { user } = await setup()
    await user.click(screen.getByRole('button', { name: /^tab$/i }))
    expect(onKey).toHaveBeenCalledWith('\t')
  })

  it('calls onKey with \\x1b[A when Arrow Up is clicked', async () => {
    const { user } = await setup()
    await user.click(screen.getByRole('button', { name: /arrow up/i }))
    expect(onKey).toHaveBeenCalledWith('\x1b[A')
  })

  it('calls onKey with \\x1b[B when Arrow Down is clicked', async () => {
    const { user } = await setup()
    await user.click(screen.getByRole('button', { name: /arrow down/i }))
    expect(onKey).toHaveBeenCalledWith('\x1b[B')
  })

  it('calls onKey with \\x1b[D when Arrow Left is clicked', async () => {
    const { user } = await setup()
    await user.click(screen.getByRole('button', { name: /arrow left/i }))
    expect(onKey).toHaveBeenCalledWith('\x1b[D')
  })

  it('calls onKey with \\x1b[C when Arrow Right is clicked', async () => {
    const { user } = await setup()
    await user.click(screen.getByRole('button', { name: /arrow right/i }))
    expect(onKey).toHaveBeenCalledWith('\x1b[C')
  })

  describe('Ctrl modifier toggle', () => {
    it('toggles visual/pressed state when Ctrl is clicked', async () => {
      const { user } = await setup()
      const ctrlBtn = screen.getByRole('button', { name: /ctrl modifier toggle/i })

      expect(ctrlBtn).toHaveAttribute('aria-pressed', 'false')
      await user.click(ctrlBtn)
      expect(ctrlBtn).toHaveAttribute('aria-pressed', 'true')
      await user.click(ctrlBtn)
      expect(ctrlBtn).toHaveAttribute('aria-pressed', 'false')
    })

    it('shows Ctrl combo buttons when Ctrl is active', async () => {
      const { user } = await setup()
      const ctrlBtn = screen.getByRole('button', { name: /ctrl modifier toggle/i })

      // Combo buttons should not be visible initially
      expect(screen.queryByRole('button', { name: /ctrl\+c/i })).not.toBeInTheDocument()

      await user.click(ctrlBtn)

      // Ctrl+C, Ctrl+D, Ctrl+Z should now appear
      expect(screen.getByRole('button', { name: /ctrl\+c/i })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /ctrl\+d/i })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /ctrl\+z/i })).toBeInTheDocument()
    })

    it('sends \\x03 (Ctrl+C) when Ctrl is active and C is clicked', async () => {
      const { user } = await setup()
      const ctrlBtn = screen.getByRole('button', { name: /ctrl modifier toggle/i })
      await user.click(ctrlBtn)

      const ctrlC = screen.getByRole('button', { name: /ctrl\+c/i })
      await user.click(ctrlC)

      expect(onKey).toHaveBeenCalledWith('\x03')
    })

    it('sends \\x04 (Ctrl+D) when Ctrl is active and D is clicked', async () => {
      const { user } = await setup()
      const ctrlBtn = screen.getByRole('button', { name: /ctrl modifier toggle/i })
      await user.click(ctrlBtn)

      const ctrlD = screen.getByRole('button', { name: /ctrl\+d/i })
      await user.click(ctrlD)

      expect(onKey).toHaveBeenCalledWith('\x04')
    })

    it('deactivates Ctrl toggle after sending a combo key', async () => {
      const { user } = await setup()
      const ctrlBtn = screen.getByRole('button', { name: /ctrl modifier toggle/i })
      await user.click(ctrlBtn)

      // Click Ctrl+C
      await user.click(screen.getByRole('button', { name: /ctrl\+c/i }))

      // Ctrl should be deactivated again
      expect(ctrlBtn).toHaveAttribute('aria-pressed', 'false')
    })
  })
})
