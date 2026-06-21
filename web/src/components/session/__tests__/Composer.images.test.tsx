import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { Composer } from '../Composer'

beforeEach(() => {
  // @ts-expect-error shim
  URL.createObjectURL = vi.fn(() => 'blob:p'); // @ts-expect-error shim
  URL.revokeObjectURL = vi.fn()
  vi.stubGlobal('fetch', vi.fn(async () =>
    ({ ok: true, json: async () => ({ sha: 'abc', mime: 'image/png', name: 'x.png' }) })))
})

function paste(el: Element, file: File) {
  const data = { files: [file], items: [{ kind: 'file', type: file.type, getAsFile: () => file }] }
  fireEvent.paste(el, { clipboardData: data })
}

describe('Composer image paste', () => {
  it('pasting an image adds a chip and includes it in the sent prompt', async () => {
    const onPrompt = vi.fn()
    render(<Composer status={{ state: 'idle', count: 0 }} mode="default"
      onPrompt={onPrompt} onInterrupt={() => {}} onSetMode={() => {}} />)
    const ta = screen.getByLabelText('Message')
    paste(ta, new File([new Uint8Array(10)], 'x.png', { type: 'image/png' }))
    await waitFor(() => screen.getByTestId('attachment-chips'))
    fireEvent.change(ta, { target: { value: 'what is this' } })
    fireEvent.click(screen.getByLabelText('Send message'))
    await waitFor(() => expect(onPrompt).toHaveBeenCalledWith('what is this',
      [{ sha: 'abc', mime: 'image/png', name: 'x.png' }]))
  })
})
