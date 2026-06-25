import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import { Toaster } from 'sonner'
import './index.css'
import App from './App.tsx'
import { queryClient } from '@/api/queryClient'
import { clientLogger } from '@/obs/logger'
import { installGlobalErrorHandlers } from '@/obs/install'

clientLogger.start()
installGlobalErrorHandlers()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
        <Toaster richColors position="top-center" theme="system" />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
