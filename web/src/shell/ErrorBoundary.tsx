// web/src/components/ErrorBoundary.tsx
import { Component, type ErrorInfo, type ReactNode } from 'react'
import { clientLogger, ClientLogger } from '@/obs/logger'

interface Props { children: ReactNode; logger?: ClientLogger }
interface State { hasError: boolean }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false }

  static getDerivedStateFromError(): State {
    return { hasError: true }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    const logger = this.props.logger ?? clientLogger
    logger.error(`react: ${error.message}`, {
      stack: error.stack,
      componentStack: info.componentStack ?? undefined,
    } as Record<string, unknown>)
  }

  render(): ReactNode {
    if (this.state.hasError) {
      return (
        <div role="alert" className="flex min-h-screen flex-col items-center justify-center gap-3 p-6 text-center">
          <p className="text-lg font-semibold">Something went wrong.</p>
          <button
            className="rounded-md border px-3 py-1.5 text-sm"
            onClick={() => window.location.reload()}
            type="button"
          >
            Reload
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
