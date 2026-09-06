import { render, screen, fireEvent } from '@testing-library/react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { Composer } from '../Composer'

// Composer looks up the session's engine (for attachment gating) via the
// sessions list cache; no test here passes a sessionId that resolves, so an
// empty list keeps `engine` undefined (unrestricted) for every case.
vi.mock('@/api/hooks', () => ({ useSessions: () => ({ data: [] }) }))

beforeEach(() => {
  localStorage.clear()
  URL.createObjectURL = vi.fn(() => 'blob:p')
  URL.revokeObjectURL = vi.fn()
})

describe('Composer draft persistence', () => {
  it('hydrates the textarea from a saved draft', () => {
    localStorage.setItem('ccswitch:draft:s1', JSON.stringify({ text: 'saved draft', attachments: [] }))
    render(<Composer status={{ state: 'idle', count: 0 }} sessionId="s1"
      onPrompt={() => {}} onInterrupt={() => {}} />)
    expect((screen.getByLabelText('Message') as HTMLTextAreaElement).value).toBe('saved draft')
  })

  it('persists typed text to localStorage', () => {
    render(<Composer status={{ state: 'idle', count: 0 }} sessionId="s1"
      onPrompt={() => {}} onInterrupt={() => {}} />)
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'work in progress' } })
    expect(JSON.parse(localStorage.getItem('ccswitch:draft:s1')!).text).toBe('work in progress')
  })

  it('clears the draft on send', () => {
    localStorage.setItem('ccswitch:draft:s1', JSON.stringify({ text: 'hello', attachments: [] }))
    const onPrompt = vi.fn()
    render(<Composer status={{ state: 'idle', count: 0 }} sessionId="s1"
      onPrompt={onPrompt} onInterrupt={() => {}} />)
    fireEvent.click(screen.getByLabelText('Send message'))
    expect(onPrompt).toHaveBeenCalledWith('hello', [])
    expect(localStorage.getItem('ccswitch:draft:s1')).toBeNull()
  })
})
