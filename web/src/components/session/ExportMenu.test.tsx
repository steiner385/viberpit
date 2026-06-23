// web/src/components/session/ExportMenu.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { ExportMenu } from './ExportMenu'

vi.mock('@/api/transcript', () => ({
  downloadTranscript: vi.fn(),
}))

import { downloadTranscript } from '@/api/transcript'

const mockDownload = downloadTranscript as ReturnType<typeof vi.fn>

beforeEach(() => {
  mockDownload.mockClear()
})

describe('ExportMenu', () => {
  it('renders an Export button', () => {
    render(<ExportMenu sessionId="sid" />)
    expect(screen.getByRole('button', { name: /export transcript/i })).toBeInTheDocument()
  })

  it('opens the menu when Export is clicked', () => {
    render(<ExportMenu sessionId="sid" />)
    expect(screen.queryByRole('menu')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /export transcript/i }))
    expect(screen.getByRole('menu')).toBeInTheDocument()
  })

  it('calls downloadTranscript with md when Markdown is picked', () => {
    render(<ExportMenu sessionId="sid" />)
    fireEvent.click(screen.getByRole('button', { name: /export transcript/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: /markdown/i }))
    expect(mockDownload).toHaveBeenCalledWith('sid', 'md')
  })

  it('calls downloadTranscript with jsonl when Raw JSONL is picked', () => {
    render(<ExportMenu sessionId="sid" />)
    fireEvent.click(screen.getByRole('button', { name: /export transcript/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: /raw jsonl/i }))
    expect(mockDownload).toHaveBeenCalledWith('sid', 'jsonl')
  })

  it('closes the menu after picking Markdown', () => {
    render(<ExportMenu sessionId="sid" />)
    fireEvent.click(screen.getByRole('button', { name: /export transcript/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: /markdown/i }))
    expect(screen.queryByRole('menu')).toBeNull()
  })

  it('closes the menu after picking Raw JSONL', () => {
    render(<ExportMenu sessionId="sid" />)
    fireEvent.click(screen.getByRole('button', { name: /export transcript/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: /raw jsonl/i }))
    expect(screen.queryByRole('menu')).toBeNull()
  })
})
