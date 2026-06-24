import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { NewVersionBanner } from '../NewVersionBanner'

describe('NewVersionBanner', () => {
  it('announces a new version', () => {
    render(<NewVersionBanner onReload={vi.fn()} />)
    expect(screen.getByText(/new version available/i)).toBeInTheDocument()
  })

  it('calls onReload when the button is clicked', async () => {
    const onReload = vi.fn()
    const user = userEvent.setup()
    render(<NewVersionBanner onReload={onReload} />)
    await user.click(screen.getByRole('button', { name: /reload/i }))
    expect(onReload).toHaveBeenCalledOnce()
  })
})
