// web/src/components/__tests__/DispatchBar.attachments.test.tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi, it, expect, beforeEach } from 'vitest'
import { DispatchBar } from '../DispatchBar'

vi.mock('@/api/client', () => ({
  uploadAttachment: vi.fn(async () => ({ id: 'a1', mediaType: 'image/png', filename: 'a.png', byteSize: 3, url: '/api/attachments/a1' })),
}))
beforeEach(() => { (globalThis as any).URL.createObjectURL = vi.fn(() => 'blob:x') })

const noop = () => {}

it('includes attachmentIds in the dispatch payload', async () => {
  const onDispatch = vi.fn()
  render(<DispatchBar repos={[]} reposLoading={false} reposError={false}
    onDispatch={onDispatch} onNavigate={noop} onOpenHighlighted={noop}
    onRowAction={noop} onShowHelp={noop} onEscape={noop} isPending={false} />)
  const file = new File([new Uint8Array([1])], 'a.png', { type: 'image/png' })
  await userEvent.upload(screen.getByLabelText(/attach image/i), file)
  await userEvent.type(screen.getByLabelText('New session prompt'), 'analyze this')
  await userEvent.keyboard('{Enter}')
  expect(onDispatch).toHaveBeenCalledWith(
    expect.objectContaining({ initialPrompt: 'analyze this', attachmentIds: ['a1'] }))
})
