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
})
