import { render, screen } from '@testing-library/react'
import { expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { PipelineTab } from '../PipelineTab'

vi.stubGlobal('EventSource', class {
  onmessage = null; onerror = null
  addEventListener() {} close() {}
} as unknown as typeof EventSource)

it('renders the loading state before the first SSE frame', () => {
  const qc = new QueryClient()
  render(<MemoryRouter><QueryClientProvider client={qc}><PipelineTab /></QueryClientProvider></MemoryRouter>)
  expect(screen.getByRole('status')).toHaveTextContent('Loading pipeline…')
})
