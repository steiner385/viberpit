import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { SessionAriaLive } from './SessionAriaLive'
import type { Turn } from '@/session/reducer'

const reply = (text: string): Turn[] => [{ id: 'a1', role: 'assistant', blocks: [{ kind: 'text', id: 'a1:t', text }] }]
const region = (c: HTMLElement) => c.querySelector('[aria-live]')!

describe('SessionAriaLive — the hand-off reason (D-11)', () => {
  it('never announces the reason a session already had when the region mounts', () => {
    const { container } = render(
      <SessionAriaLive turns={reply('hi')} replaying={false} handOff={{ label: 'Handed back', reason: 'Need a key' }} />,
    )
    expect(region(container).textContent).not.toContain('Need a key')
  })

  it('announces "<label> — <reason>" once when the pair changes, not again on later renders', () => {
    const { container, rerender } = render(<SessionAriaLive turns={reply('hi')} replaying={false} handOff={null} />)
    rerender(<SessionAriaLive turns={reply('hi')} replaying={false} handOff={{ label: 'Asked you', reason: 'Which branch?' }} />)
    expect(region(container).textContent).toBe('Asked you — Which branch?')

    // A later render with the same pair — even with new output — does not re-announce it.
    rerender(<SessionAriaLive turns={reply('working on it')} replaying={false} handOff={{ label: 'Asked you', reason: 'Which branch?' }} />)
    expect(region(container).textContent).toBe('working on it')
  })

  it('a new reason, or the same reason under a new mode, is announced again', () => {
    const { container, rerender } = render(<SessionAriaLive turns={reply('hi')} replaying={false} handOff={null} />)
    rerender(<SessionAriaLive turns={reply('hi')} replaying={false} handOff={{ label: 'Asked you', reason: 'Which branch?' }} />)
    rerender(<SessionAriaLive turns={reply('hi')} replaying={false} handOff={{ label: 'Handed back', reason: 'Which branch?' }} />)
    expect(region(container).textContent).toBe('Handed back — Which branch?')
  })
})
