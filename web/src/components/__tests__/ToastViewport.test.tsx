import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ToastViewport } from '../ToastViewport'
import { toast, getToasts, __resetToasts } from '@/lib/toast'
import { act } from 'react'

describe('ToastViewport', () => {
  beforeEach(() => { __resetToasts() })

  it('renders nothing when empty', () => {
    const { container } = render(<ToastViewport />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders a toast message and dismisses on the × button', () => {
    render(<ToastViewport />)
    act(() => { toast({ message: 'Archived Foo', variant: 'success' }) })
    expect(screen.getByText('Archived Foo')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByText('Archived Foo')).not.toBeInTheDocument()
  })

  it('fires the action callback and dismisses', () => {
    const onClick = vi.fn()
    render(<ToastViewport />)
    act(() => { toast({ message: 'Archived Foo', action: { label: 'Undo', onClick } }) })
    fireEvent.click(screen.getByRole('button', { name: 'Undo' }))
    expect(onClick).toHaveBeenCalledTimes(1)
    expect(getToasts()).toHaveLength(0)
  })
})
