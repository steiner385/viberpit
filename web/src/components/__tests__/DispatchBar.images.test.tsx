import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { it, expect, vi, beforeEach } from 'vitest'
import { DispatchBar } from '../DispatchBar'

beforeEach(() => {
  // @ts-expect-error shim
  URL.createObjectURL = vi.fn(() => 'blob:p'); // @ts-expect-error shim
  URL.revokeObjectURL = vi.fn()
  vi.stubGlobal('fetch', vi.fn(async () =>
    ({ ok: true, json: async () => ({ id: 'att-1', mediaType: 'image/png', filename: 'x.png', byteSize: 10, url: '/api/attachments/att-1' }) })))
})

it('dispatch includes pasted image attachment ids', async () => {
  const onDispatch = vi.fn()
  render(<DispatchBar repos={[]} reposLoading={false} reposError={false} onDispatch={onDispatch} />)
  const ta = screen.getByRole('textbox')
  fireEvent.paste(ta, { clipboardData: {
    files: [new File([new Uint8Array(10)], 'x.png', { type: 'image/png' })] } })
  await waitFor(() => screen.getByTestId('attachment-chips'))
  fireEvent.change(ta, { target: { value: 'start with this' } })
  fireEvent.keyDown(ta, { key: 'Enter' })
  await waitFor(() => expect(onDispatch).toHaveBeenCalledWith(expect.objectContaining({
    initialPrompt: 'start with this',
    attachmentIds: ['att-1'] })))
})

it('dispatch with image-only (empty prompt) calls onDispatch not onOpenHighlighted', async () => {
  const onDispatch = vi.fn()
  const onOpenHighlighted = vi.fn()
  render(
    <DispatchBar
      repos={[]}
      reposLoading={false}
      reposError={false}
      onDispatch={onDispatch}
      onOpenHighlighted={onOpenHighlighted}
      onNavigate={vi.fn()}
      onRowAction={vi.fn()}
      onShowHelp={vi.fn()}
      onEscape={vi.fn()}
      isPending={false}
    />,
  )
  const ta = screen.getByRole('textbox')
  fireEvent.paste(ta, { clipboardData: {
    files: [new File([new Uint8Array(10)], 'x.png', { type: 'image/png' })] } })
  await waitFor(() => screen.getByTestId('attachment-chips'))
  fireEvent.keyDown(ta, { key: 'Enter' })
  await waitFor(() => expect(onDispatch).toHaveBeenCalledWith(expect.objectContaining({
    initialPrompt: '',
    attachmentIds: ['att-1'] })))
  expect(onOpenHighlighted).not.toHaveBeenCalled()
})
