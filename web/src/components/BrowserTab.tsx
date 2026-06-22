import { useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import { VncClient, type VncState } from '../vnc/vncClient'

const COPY = {
  disabled: "Browser sharing isn't enabled on this server.",
  nodisplay: 'No display was allocated for this session.',
  empty: "The agent hasn't opened a browser yet. When it does, you'll see it here.",
  reconnecting: 'Reconnecting…',
  disconnected: 'Disconnected — tap to reconnect.',
  connecting: 'Connecting…',
}

interface Props {
  sessionId: string
  statusState: string
  initialState?: VncState
}

export function BrowserTab({ sessionId, statusState, initialState }: Props) {
  const canvasRef = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<VncState>(initialState ?? 'connecting')
  const [fit, setFit] = useState(true)
  const [focused, setFocused] = useState(false)
  const clientRef = useRef<VncClient | null>(null)

  useEffect(() => {
    if (initialState) return // test/storybook injection — skip real connect
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    const url = `${proto}://${location.host}/ws/vnc?session=${encodeURIComponent(sessionId)}`
    const c = new VncClient(url)
    clientRef.current = c
    c.on('connect', () => setState('connected'))
    c.on('reconnecting', () => setState('reconnecting'))
    c.on('disconnect', (s) => setState(s))
    if (canvasRef.current) c.connect(canvasRef.current)
    return () => c.disconnect()
  }, [sessionId, initialState])

  async function openBrowser() {
    await fetch(`/api/sessions/${sessionId}/browser/open`, { method: 'POST' })
  }

  const agentDriving = statusState === 'working'

  // Blocking overlay states: transitional or terminal — never 'connected'
  const blockingOverlay: { copy: string } | null =
    state === 'connecting'
      ? { copy: COPY.connecting }
      : state === 'reconnecting'
        ? { copy: COPY.reconnecting }
        : state === 'disconnected'
          ? { copy: COPY.disconnected }
          : state === 'disabled'
            ? { copy: COPY.disabled }
            : state === 'nodisplay'
              ? { copy: COPY.nodisplay }
              : null

  return (
    <div className="relative h-full w-full overflow-hidden">
      {/* noVNC canvas container — always rendered so the RFB client has a stable mount */}
      <div
        ref={canvasRef}
        className="h-full w-full"
        tabIndex={0}
        onFocus={() => {
          setFocused(true)
          clientRef.current?.focus()
        }}
        onBlur={() => {
          setFocused(false)
          clientRef.current?.blur()
        }}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.stopPropagation()
            ;(e.currentTarget as HTMLElement).blur()
          }
        }}
      />

      {/* Agent-driving badge */}
      {agentDriving && (
        <div
          className={cn(
            'pointer-events-none absolute left-2 top-2',
            'rounded bg-amber-500/90 px-2 py-1 text-xs font-medium text-white',
          )}
        >
          Agent is driving
        </div>
      )}

      {/* Fit / 1:1 toggle — always visible, ≥44px touch target */}
      <button
        className={cn(
          'absolute right-2 top-2 min-h-[44px] min-w-[44px]',
          'rounded border border-border bg-background/80 px-2 text-xs',
        )}
        onClick={() => {
          const f = !fit
          setFit(f)
          clientRef.current?.setScale(f)
        }}
      >
        {fit ? '1:1' : 'Fit'}
      </button>

      {/* "Tap to control" caption — shown when connected and canvas not focused */}
      {state === 'connected' && !focused && (
        <div className="pointer-events-none absolute inset-x-0 bottom-2 text-center text-xs text-muted-foreground">
          Tap to control · pinch to zoom
        </div>
      )}

      {/* Blocking full-screen overlay for transitional/terminal states only */}
      {blockingOverlay && (
        <div className="absolute inset-0 flex flex-col items-center justify-center bg-background/80 p-6 text-center text-sm text-muted-foreground">
          {blockingOverlay.copy}
        </div>
      )}

      {/* Non-blocking compact hint when connected but framebuffer is blank.
          Pinned to bottom-center, does NOT cover the canvas — pointer-events
          only on the button so users can click through the hint area. */}
      {state === 'connected' && (
        <div className="pointer-events-none absolute inset-x-0 bottom-8 flex flex-col items-center gap-2">
          <p className="text-xs text-muted-foreground drop-shadow">
            {COPY.empty}
          </p>
          <button
            className={cn(
              'pointer-events-auto',
              'rounded bg-primary px-3 py-2 text-sm text-primary-foreground shadow',
            )}
            onClick={openBrowser}
          >
            Open a browser
          </button>
        </div>
      )}
    </div>
  )
}
