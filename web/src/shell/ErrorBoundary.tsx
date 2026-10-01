// web/src/shell/ErrorBoundary.tsx
import { Component, type ErrorInfo, type ReactNode } from 'react'
import { Button } from '@/ui/Button'
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
      // On the tokens and the primitives like every other surface: this was the
      // one screen left on `min-h-screen`, a raw `border` and a bare <button>
      // (2026-10-01 shell critique, minor). `min-h-dvh`, not `-screen`: the
      // shell's own column is `100dvh`, so the fallback fills the same box.
      // The error has already been logged above; saying so is the one fact the
      // operator can use, and a reload is the one action.
      return (
        <div role="alert" className="flex min-h-dvh flex-col items-center justify-center gap-3 bg-canvas p-6 text-center text-ink">
          <p className="text-lg font-semibold">Something went wrong.</p>
          <p className="max-w-prose text-sm text-ink-muted">The error has been logged. Reloading does not touch your sessions.</p>
          <Button type="button" variant="secondary" size="lg" onClick={() => window.location.reload()}>
            Reload
          </Button>
        </div>
      )
    }
    return this.props.children
  }
}
