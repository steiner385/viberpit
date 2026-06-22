import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ThinkingBlock } from '../ThinkingBlock'

describe('ThinkingBlock', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('is collapsed by default — text not visible', () => {
    render(<ThinkingBlock text="secret thought" defaultCollapsed={true} />)
    // The region should be hidden
    const toggle = screen.getByRole('button', { name: /thinking/i })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })

  it('hides content region by default', () => {
    render(<ThinkingBlock text="hidden content" defaultCollapsed={true} />)
    const regionId = screen.getByRole('button', { name: /thinking/i }).getAttribute('aria-controls')!
    const region = document.getElementById(regionId)
    expect(region).not.toBeNull()
    expect(region!.hidden).toBe(true)
  })

  it('reveals content and flips aria-expanded on toggle click', async () => {
    const user = userEvent.setup()
    render(<ThinkingBlock text="revealed thought" defaultCollapsed={true} />)
    const toggle = screen.getByRole('button', { name: /thinking/i })
    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    const regionId = toggle.getAttribute('aria-controls')!
    expect(document.getElementById(regionId)!.hidden).toBe(false)
    expect(screen.getByText('revealed thought')).toBeInTheDocument()
  })

  it('collapses again on second click', async () => {
    const user = userEvent.setup()
    render(<ThinkingBlock text="toggle me" defaultCollapsed={true} />)
    const toggle = screen.getByRole('button', { name: /thinking/i })
    await user.click(toggle)
    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
  })
})

describe('ThinkingBlock defaultCollapsed', () => {
  it('starts collapsed when defaultCollapsed is true', () => {
    render(<ThinkingBlock text="secret reasoning" defaultCollapsed={true} />)
    expect(screen.getByRole('button', { name: /thinking/i })).toHaveAttribute('aria-expanded', 'false')
  })

  it('starts expanded when defaultCollapsed is false', () => {
    render(<ThinkingBlock text="secret reasoning" defaultCollapsed={false} />)
    expect(screen.getByRole('button', { name: /thinking/i })).toHaveAttribute('aria-expanded', 'true')
  })

  it('a manual toggle overrides the default', async () => {
    const user = userEvent.setup()
    render(<ThinkingBlock text="secret reasoning" defaultCollapsed={true} />)
    await user.click(screen.getByRole('button', { name: /thinking/i }))
    expect(screen.getByRole('button', { name: /thinking/i })).toHaveAttribute('aria-expanded', 'true')
  })
})
