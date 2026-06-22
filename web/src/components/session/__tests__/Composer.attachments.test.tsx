import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi, it, expect, beforeEach } from 'vitest'
import { Composer } from '../Composer'

vi.mock('@/api/client', () => ({
  uploadAttachment: vi.fn(async () => ({ id: 'a1', mediaType: 'image/png', filename: 'a.png', byteSize: 3, url: '/api/attachments/a1' })),
}))
beforeEach(() => { (globalThis as any).URL.createObjectURL = vi.fn(() => 'blob:x') })

it('forwards uploaded attachment ids on submit', async () => {
  const onPrompt = vi.fn()
  render(<Composer status={{ state: 'idle', count: 0 }} mode="default"
    onPrompt={onPrompt} onInterrupt={() => {}} onSetMode={() => {}} />)
  const file = new File([new Uint8Array([1])], 'a.png', { type: 'image/png' })
  const input = screen.getByLabelText(/attach image/i) as HTMLInputElement
  await userEvent.upload(input, file)
  await userEvent.type(screen.getByLabelText('Message'), 'look')
  await userEvent.click(screen.getByRole('button', { name: /send message/i }))
  expect(onPrompt).toHaveBeenCalledWith('look', ['a1'])
})
