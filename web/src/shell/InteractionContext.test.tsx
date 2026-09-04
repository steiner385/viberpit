import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { InteractionProvider, useInteractive } from './InteractionContext'

function Probe() {
  return <span data-testid="i">{String(useInteractive())}</span>
}

describe('InteractionContext', () => {
  it('defaults to interactive outside a provider', () => {
    render(<Probe />)
    expect(screen.getByTestId('i').textContent).toBe('true')
  })

  it('reports false inside a non-interactive provider', () => {
    render(<InteractionProvider interactive={false}><Probe /></InteractionProvider>)
    expect(screen.getByTestId('i').textContent).toBe('false')
  })

  it('reports true inside an interactive provider', () => {
    render(<InteractionProvider interactive={true}><Probe /></InteractionProvider>)
    expect(screen.getByTestId('i').textContent).toBe('true')
  })

  it('the innermost provider wins', () => {
    render(
      <InteractionProvider interactive={false}>
        <InteractionProvider interactive={true}><Probe /></InteractionProvider>
      </InteractionProvider>,
    )
    expect(screen.getByTestId('i').textContent).toBe('true')
  })
})
