import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { DispatchBar } from '../DispatchBar'

beforeEach(() => {
  // @ts-expect-error shim
  URL.createObjectURL = vi.fn(() => 'blob:p'); // @ts-expect-error shim
  URL.revokeObjectURL = vi.fn()
  vi.stubGlobal('fetch', vi.fn(async () =>
    ({ ok: true, json: async () => ({ sha: 'abc', mime: 'image/png', name: 'x.png' }) })))
})

it('dispatch includes pasted image refs', async () => {
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
    images: [{ sha: 'abc', mime: 'image/png', name: 'x.png' }] })))
})
