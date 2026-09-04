import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { NewVersionBanner } from '../NewVersionBanner'

describe('NewVersionBanner', () => {
  it('announces a new version', () => {
    render(<NewVersionBanner onReload={vi.fn()} />)
    expect(screen.getByText(/new version available/i)).toBeInTheDocument()
  })

  it('renders in normal flow, never as a fixed overlay', () => {
    // It lives in the shell's BannerStack. A `fixed top-0` copy sat on the same
    // band as the toaster and the page header; in flow it pushes content down.
    render(<NewVersionBanner onReload={vi.fn()} />)
    const el = screen.getByRole('status')
    expect(el.className).not.toMatch(/\bfixed\b/)
    expect(el.className).not.toMatch(/\bz-\S+/)
  })

  it('calls onReload when the button is clicked', async () => {
    const onReload = vi.fn()
    const user = userEvent.setup()
    render(<NewVersionBanner onReload={onReload} />)
    await user.click(screen.getByRole('button', { name: /reload/i }))
    expect(onReload).toHaveBeenCalledOnce()
  })
})
