import { render, screen } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { ModelSwitchDialog } from '../ModelSwitchDialog'

/**
 * Touch-target regression guard. docs/ACCESSIBILITY.md commits to >= 44x44 px
 * on every interactive element, and Button only carries `min-h-target` on its
 * `lg` size (`sm` is 36px, `md` 40px). Two of these three buttons previously
 * passed no size and rendered at 40px. jsdom computes no layout, so we pin the
 * mechanism — the `lg` size classes — rather than a measured height.
 */
const LG_CLASSES = ['h-11', 'min-h-target']

describe('ModelSwitchDialog', () => {
  const props = { modelLabel: 'Opus 4.8', onClose: vi.fn(), onConfirm: vi.fn() }

  it('gives every action button a 44px touch target', () => {
    render(<ModelSwitchDialog {...props} />)
    for (const name of [/cancel/i, /switch after this turn/i, /stop & switch now/i]) {
      const btn = screen.getByRole('button', { name })
      for (const cls of LG_CLASSES) {
        expect(btn.className, `"${btn.textContent}" is missing ${cls}`).toContain(cls)
      }
    }
  })

  it('renders all three actions with the target model named', () => {
    render(<ModelSwitchDialog {...props} />)
    expect(screen.getByText(/Opus 4\.8/)).toBeInTheDocument()
    expect(screen.getAllByRole('button')).toHaveLength(3)
  })

  // The non-interrupting choice sends the same `after_turn` command for both
  // engines, but the two engines DO different things with it: without the
  // `steer` capability the SDK applies the model to the NEXT turn, while an
  // engine that can steer retargets the turn already running
  // (server/codex/driver.py `set_model` → `turn/settings/update`). The label has
  // to say which, and it is gated on the capability, never on the engine name.
  describe('an engine that can steer a running turn', () => {
    it('says the running turn is retargeted rather than left to finish', () => {
      render(<ModelSwitchDialog {...props} canSteer />)
      expect(screen.getByRole('button', { name: /switch without stopping/i })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /switch after this turn/i })).toBeNull()
    })

    it('still sends the same after_turn decision', () => {
      const onConfirm = vi.fn()
      render(<ModelSwitchDialog {...props} onConfirm={onConfirm} canSteer />)
      screen.getByRole('button', { name: /switch without stopping/i }).click()
      expect(onConfirm).toHaveBeenCalledWith('after_turn')
    })

    it('keeps every button at the 44px touch target', () => {
      render(<ModelSwitchDialog {...props} canSteer />)
      for (const btn of screen.getAllByRole('button')) {
        for (const cls of LG_CLASSES) {
          expect(btn.className, `"${btn.textContent}" is missing ${cls}`).toContain(cls)
        }
      }
    })
  })
})
