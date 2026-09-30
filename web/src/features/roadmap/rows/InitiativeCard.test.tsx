import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ViewportProvider } from '@/shell/useViewport'
import { ini } from '../__fixtures__/map'
import { InitiativeCard } from './InitiativeCard'

// 2026-09-30 Roadmap critique, P0: in the Initiatives scope every row showed
// only a grey "Part of <program>" badge — the badge sat unconstrained in the
// card's trailing slot beside the stage chip, took the row's width (192px,
// wrapping), and the identity `truncate` span collapsed to 0px, so the surface
// named for "what are we building, in what order?" showed no names. The program
// now opens the subtitle line as prose; line 2 truncates as a whole and the
// title on line 1 always wins. jsdom has no layout, so the contract here is
// structural: the title is in the identity, the program is in the subtitle,
// and nothing but the stage chip is trailing.
function renderCard(programTitle: string | null) {
  const initiative = ini({
    slug: 'restore', projectSlug: 'kindash', title: '01 Restore', stage: 'spec',
    tier: 'needs-you', reason: 'advance', priority: 16,
    epic: { ref: 'o/r#12', url: null, total: 4, done: 1, blocked: 0, held: 0 },
  })
  return render(
    <ViewportProvider kiosk={null}>
      <MemoryRouter>
        <InitiativeCard
          initiative={initiative} rowKey="ini:restore" depth={0} selected={false}
          onOpen={vi.fn()} rowRole="listitem" idScope="t" programTitle={programTitle}
        />
      </MemoryRouter>
    </ViewportProvider>,
  )
}

describe('InitiativeCard — a phase row keeps its own name', () => {
  it('puts the program in the subtitle line, not a trailing badge, so the title stays visible', () => {
    renderCard('UI audit — September 2026 benchmark and systemic gap closure')
    // The title is the identity, on line 1.
    expect(screen.getByRole('button', { name: 'Open initiative 01 Restore' })).toHaveTextContent('01 Restore')
    // The program CLOSES line 2 as prose, after the reason and progress: the line
    // truncates as a whole, so what a human acts on keeps its place and a long
    // program name is what gives way.
    expect(screen.getByText(/1\/4 done · priority 16 · Part of UI audit — September 2026 benchmark and systemic gap closure$/)).toBeInTheDocument()
    // No element carries "Part of …" as a title/aria-label any more: the old badge is gone.
    expect(screen.queryByTitle(/^Part of/)).toBeNull()
    expect(screen.queryByLabelText(/^Part of/)).toBeNull()
  })

  it('omits the program prefix for a row that has none (Map tree, detail lists)', () => {
    renderCard(null)
    expect(screen.queryByText(/Part of/)).toBeNull()
    expect(screen.getByText(/1\/4 done · priority 16/)).toBeInTheDocument()
  })
})
