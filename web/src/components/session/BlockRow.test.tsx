import { render } from '@testing-library/react'
import { BlockRow } from './BlockRow'

test('renders a time element with title when ts present', () => {
  const { container } = render(<BlockRow ts={Date.UTC(2026, 5, 23, 14, 32, 7)}><span>hi</span></BlockRow>)
  const time = container.querySelector('time')!
  expect(time.textContent).toMatch(/\d{1,2}:\d{2}/)
  expect(time.getAttribute('title')).toBeTruthy()
})

test('absent ts renders an aligned empty gutter (no Invalid Date)', () => {
  const { container } = render(<BlockRow><span>hi</span></BlockRow>)
  expect(container.textContent).not.toMatch(/Invalid/)
  expect(container.querySelector('time')).not.toBeNull() // gutter slot still present
})
