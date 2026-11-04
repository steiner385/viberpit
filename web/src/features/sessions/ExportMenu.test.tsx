// web/src/features/sessions/ExportMenu.test.tsx
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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

  it('asks before the unredacted Raw JSONL download, then downloads on confirm', async () => {
    render(<ExportMenu sessionId="sid" />)
    fireEvent.click(screen.getByRole('button', { name: /export transcript/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: /raw jsonl/i }))
    const dialog = await screen.findByRole('dialog', { name: /download the raw transcript/i })
    expect(dialog).toHaveTextContent(/may contain secrets/i)
    expect(mockDownload).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: /download raw/i }))
    expect(mockDownload).toHaveBeenCalledWith('sid', 'jsonl')
  })

  it('does not download Raw JSONL when the warning is cancelled', async () => {
    render(<ExportMenu sessionId="sid" />)
    fireEvent.click(screen.getByRole('button', { name: /export transcript/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: /raw jsonl/i }))
    await userEvent.click(await screen.findByRole('button', { name: /cancel/i }))
    expect(mockDownload).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('downloads Markdown without a confirmation (it is redacted)', () => {
    render(<ExportMenu sessionId="sid" />)
    fireEvent.click(screen.getByRole('button', { name: /export transcript/i }))
    fireEvent.click(screen.getByRole('menuitem', { name: /markdown/i }))
    expect(screen.queryByRole('dialog')).toBeNull()
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
