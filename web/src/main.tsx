import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { Toaster } from 'sonner'
import './index.css'
import App from './App.tsx'
import { queryClient } from '@/api/queryClient'
import { clientLogger } from '@/obs/logger'
import { installGlobalErrorHandlers } from '@/obs/install'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import { ThemeProvider } from '@/shell/theme'

clientLogger.start()
installGlobalErrorHandlers()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        {/* `App` owns the router now (`createBrowserRouter` + `RouterProvider`),
            so there is no `<BrowserRouter>` here — having both is an error.

            `Toaster` therefore sits OUTSIDE the router as well as outside
            `<ErrorBoundary>`. Both are deliberate. Outside the boundary,
            because a crash anywhere in the app tree must not unmount the one
            surface left to report it. Outside the router is safe because
            nothing in the toast path uses a router hook: every call site passes
            a string to `toast()` / `toast.error()` — verified across
            `PromptDock`, `GeneralSection` and the rest — so no toast renders a
            `<Link>` or reads `useNavigate`. If a toast ever needs to navigate,
            it has to move inside the router, not the other way round. */}
        <ErrorBoundary>
          <App />
        </ErrorBoundary>
        <Toaster richColors position="top-center" theme="system" />
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
)
