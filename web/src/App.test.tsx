import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import App from './App'

function wrapper({ children }: { children: React.ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

describe('App', () => {
  it('renders the ccswitch heading', () => {
    render(<App />, { wrapper })
    expect(screen.getByRole('heading', { name: /ccswitch/i })).toBeInTheDocument()
  })
})
