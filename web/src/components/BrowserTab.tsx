import { useEffect, useRef, useState } from 'react'
import { ClipboardPaste } from 'lucide-react'
import { cn } from '@/lib/utils'
import { VncClient, type VncState } from '../vnc/vncClient'
import { useZoom } from '../vnc/useZoom'
import { statusTier } from '../session/statusTier'
import { useMediaQuery } from '@/lib/useMediaQuery'
import { relayInput, relayKeydown } from '../vnc/keyRelay'
import { pasteClipboardToSession } from '../vnc/clipboardPaste'

const COPY = {
  disabled: "Browser sharing isn't enabled on this server.",
  // A session only lacks a display when it has no live process. The common case is
  // an idle/detached session (its virtual display is allocated lazily when it next
  // runs), so point the user at the action that fixes it rather than at the raw
  // internal state. The 'busy' fallback covers the rare pool-exhausted case, where
  // "send a prompt" would be wrong advice.
  nodisplayIdle: "This session isn't running. Send it a prompt to start it, and the virtual browser will appear here.",
  nodisplayBusy: 'No virtual display is available for this session right now.',
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
  const panRef = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<VncState>(initialState ?? 'connecting')
  const [focused, setFocused] = useState(false)
  const [emptyDismissed, setEmptyDismissed] = useState(false)
  const [openError, setOpenError] = useState<string | null>(null)
  const clientRef = useRef<VncClient | null>(null)
  const isTouch = useMediaQuery('(pointer: coarse)')
  const [keyboardOpen, setKeyboardOpen] = useState(false)
  const hiddenInputRef = useRef<HTMLInputElement>(null)

  // Push a key into the live session; no-ops when not connected.
  const send = (keysym: number, code: string | null, down?: boolean) =>
    clientRef.current?.sendKey(keysym, code, down)

  // Paste-from-clipboard: type the user's client clipboard into the focused
  // remote field (e.g. a password box in the login window). Typed via keysyms,
  // so it lands even when the remote X session has no clipboard manager.
  const [pasteHint, setPasteHint] = useState<string | null>(null)
  const pasteHintTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  useEffect(() => () => clearTimeout(pasteHintTimer.current), [])

  function showPasteHint(msg: string) {
    setPasteHint(msg)
    clearTimeout(pasteHintTimer.current)
    pasteHintTimer.current = setTimeout(() => setPasteHint(null), 2500)
  }

  // Paste-capture fallback: the async Clipboard API only exists in secure
  // contexts (HTTPS/localhost), so on a plain-http tailnet origin
  // navigator.clipboard is undefined and readText can never run. The `paste`
  // ClipboardEvent has no such restriction — the user performs the gesture —
  // so we surface a small field to paste into and relay its text as keystrokes.
  const [pasteCapture, setPasteCapture] = useState(false)
  const pasteCaptureRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (pasteCapture) pasteCaptureRef.current?.focus()
  }, [pasteCapture])

  async function pasteFromClipboard() {
    if (!navigator.clipboard?.readText) {
      setPasteCapture(true)
      return
    }
    const result = await pasteClipboardToSession(() => navigator.clipboard.readText(), send)
    if (result === 'denied') {
      setPasteCapture(true)
      return
    }
    showPasteHint(result === 'empty' ? 'Clipboard is empty.' : 'Pasted into the session.')
  }

  function relayCapturedPaste(text: string) {
    setPasteCapture(false)
    if (text) {
      relayInput(text, send)
      showPasteHint('Pasted into the session.')
    }
    canvasRef.current?.focus()
  }

  function toggleKeyboard() {
    const next = !keyboardOpen
    setKeyboardOpen(next)
    if (next) hiddenInputRef.current?.focus()
    else hiddenInputRef.current?.blur()
  }
  const { zoom, zoomIn, zoomOut, setZoom, resetFit, zoomed, label } = useZoom()

  // Active pointers for two-finger pinch-to-zoom on the pan container.
  const pointersRef = useRef<Map<number, { x: number; y: number }>>(new Map())
  const pinchRef = useRef<{ dist: number; zoom: number } | null>(null)

  function pointerDistance() {
    const pts = Array.from(pointersRef.current.values())
    if (pts.length < 2) return 0
    const [a, b] = pts
    return Math.hypot(a.x - b.x, a.y - b.y)
  }

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

  // Re-fit the VNC view when the pan container resizes (debounced ~150ms).
  useEffect(() => {
    const el = panRef.current
    if (!el) return
    let t: ReturnType<typeof setTimeout> | undefined
    const ro = new ResizeObserver(() => {
      clearTimeout(t)
      t = setTimeout(() => clientRef.current?.refit(), 150)
    })
    ro.observe(el)
    return () => { clearTimeout(t); ro.disconnect() }
  }, [])

  async function openBrowser() {
    setOpenError(null)
    try {
      const r = await fetch(`/api/sessions/${sessionId}/browser/open`, { method: 'POST' })
      if (!r.ok) {
        // The endpoint returns 409 for both "no display" and "browser cap reached";
        // distinguish so the user gets actionable copy instead of a silent no-op.
        const body = await r.json().catch(() => ({}))
        setOpenError(
          body?.detail === 'browser cap reached'
            ? 'Too many browser windows are already open for this session.'
            : "Couldn't open a browser for this session.",
        )
      }
    } catch {
      setOpenError("Couldn't reach the server to open a browser.")
    }
  }

  const agentDriving = statusState === 'working'
  // Empty-state hint: visible when connected and not yet dismissed.
  // Dismissible because there is no per-session browser-open signal on the client.
  const showEmpty = state === 'connected' && !emptyDismissed

  function dismissEmpty() {
    setEmptyDismissed(true)
    canvasRef.current?.focus()
  }

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
              ? { copy: statusTier(statusState) === 'busy' ? COPY.nodisplayBusy : COPY.nodisplayIdle }
              : null

  return (
    <div className="relative h-full w-full overflow-hidden">
      {/* Hidden editable proxy: focusing it summons the OS soft keyboard on touch
          devices (a <canvas> never does). Keystrokes relay into noVNC. Kept empty
          so it never enters an IME composition state. */}
      <input
        ref={hiddenInputRef}
        aria-label="Keyboard input"
        className="absolute h-px w-px opacity-0"
        style={{ left: -9999, top: 0 }}
        autoCapitalize="none"
        autoCorrect="off"
        autoComplete="off"
        spellCheck={false}
        onInput={(e) => {
          relayInput(e.currentTarget.value, send)
          e.currentTarget.value = ''
        }}
        onKeyDown={(e) => {
          if (relayKeydown(e, send)) e.preventDefault()
        }}
        onBlur={() => setKeyboardOpen(false)}
      />
      {/* Pan container — produces real scrollbars to pan the zoomed canvas. */}
      <div
        ref={panRef}
        className="h-full w-full overflow-auto"
        onPointerDown={(e) => {
          pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
          if (pointersRef.current.size === 2) {
            pinchRef.current = { dist: pointerDistance(), zoom }
          }
        }}
        onPointerMove={(e) => {
          if (!pointersRef.current.has(e.pointerId)) return
          pointersRef.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
          if (pointersRef.current.size === 2 && pinchRef.current && pinchRef.current.dist > 0) {
            const ratio = pointerDistance() / pinchRef.current.dist
            setZoom(pinchRef.current.zoom * ratio)
          }
        }}
        onPointerUp={(e) => {
          pointersRef.current.delete(e.pointerId)
          if (pointersRef.current.size < 2) pinchRef.current = null
        }}
        onPointerCancel={(e) => {
          pointersRef.current.delete(e.pointerId)
          if (pointersRef.current.size < 2) pinchRef.current = null
        }}
      >
        {/* Zoom layer — sized zoom×100% so noVNC's scaleViewport autoscale fills
            it (sharper than CSS upscaling) and overflow-auto yields pan scrollbars.
            Magnification is driven by SIZE ALONE: a transform: scale() here would
            be measured a second time by noVNC's getBoundingClientRect, netting
            ~zoom² on-screen. See web/node_modules/@novnc/novnc/core/display.js
            autoscale (no upscale clamp) + rfb.js _screenSize (getBoundingClientRect). */}
        <div
          data-testid="zoom-layer"
          className="transition-[width,height] motion-reduce:transition-none"
          style={{
            width: `${zoom * 100}%`,
            height: `${zoom * 100}%`,
          }}
        >
          {/* noVNC canvas container — always rendered so the RFB client has a stable mount */}
          <div
            ref={canvasRef}
            className="h-full w-full"
            tabIndex={0}
            aria-label="Live browser view"
            onWheel={(e) => {
              if (e.ctrlKey || e.metaKey) {
                e.preventDefault()
                setZoom(zoom * (1 - e.deltaY * 0.001))
              }
            }}
            onFocus={() => {
              setFocused(true)
              clientRef.current?.focus()
            }}
            onBlur={() => {
              setFocused(false)
              clientRef.current?.blur()
            }}
            onKeyDown={(e) => {
              if (e.key === '+' || e.key === '=') zoomIn()
              else if (e.key === '-') zoomOut()
              else if (e.key === 'Escape') {
                e.stopPropagation()
                ;(e.currentTarget as HTMLElement).blur()
              }
            }}
          />
        </div>
      </div>

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

      {/* Top-right zoom cluster — one group, ≥44px targets */}
      <div className="absolute right-2 top-2 flex items-center gap-1">
        <button
          aria-label="Paste from clipboard"
          title="Paste your clipboard into the session"
          className={cn(
            'min-h-[44px] min-w-[44px] flex items-center justify-center',
            'rounded border border-edge bg-canvas/80 px-2',
          )}
          onClick={pasteFromClipboard}
        >
          <ClipboardPaste size={16} aria-hidden="true" />
        </button>
        {isTouch && (
          <button
            aria-label={keyboardOpen ? 'Hide keyboard' : 'Show keyboard'}
            aria-pressed={keyboardOpen}
            className={cn(
              'min-h-[44px] min-w-[44px]',
              'rounded border border-edge bg-canvas/80 px-2 text-base leading-none',
              keyboardOpen && 'bg-accent text-accent-ink',
            )}
            onClick={toggleKeyboard}
          >
            ⌨
          </button>
        )}
        <button
          aria-label="Zoom out"
          className={cn(
            'min-h-[44px] min-w-[44px]',
            'rounded border border-edge bg-canvas/80 px-2 text-base leading-none',
          )}
          onClick={zoomOut}
        >
          −
        </button>
        <span
          aria-live="polite"
          className="rounded bg-canvas/80 px-2 text-xs tabular-nums"
        >
          {label}
        </span>
        <button
          aria-label="Zoom in"
          className={cn(
            'min-h-[44px] min-w-[44px]',
            'rounded border border-edge bg-canvas/80 px-2 text-base leading-none',
          )}
          onClick={zoomIn}
        >
          +
        </button>
        {zoomed && (
          <button
            aria-label="Reset to fit"
            className={cn(
              'min-h-[44px] min-w-[44px]',
              'rounded border border-edge bg-canvas/80 px-2 text-xs',
            )}
            onClick={resetFit}
          >
            Fit
          </button>
        )}
      </div>

      {/* Paste-capture fallback field — the user pastes here with the native
          gesture (Ctrl+V / long-press → Paste); the text relays as keystrokes
          and is never stored. Closes on paste, Escape, or focus loss. */}
      {pasteCapture && (
        <input
          ref={pasteCaptureRef}
          aria-label="Paste target"
          placeholder="Paste here (Ctrl+V / long-press)"
          className="absolute right-2 top-14 w-64 rounded border border-edge bg-canvas px-2 py-2 text-sm shadow"
          autoCapitalize="none"
          autoCorrect="off"
          autoComplete="off"
          spellCheck={false}
          onPaste={(e) => {
            e.preventDefault()
            relayCapturedPaste(e.clipboardData.getData('text'))
          }}
          onInput={(e) => {
            // Some mobile keyboards insert clipboard suggestions as plain input
            // (no paste event) — relay whatever lands here the same way.
            const v = e.currentTarget.value
            e.currentTarget.value = ''
            if (v) relayCapturedPaste(v)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              e.stopPropagation()
              setPasteCapture(false)
            }
          }}
          onBlur={() => setPasteCapture(false)}
        />
      )}

      {/* Transient paste feedback (auto-clears) — sits just under the button cluster. */}
      {pasteHint && (
        <div
          role="status"
          className="absolute right-2 top-14 rounded bg-canvas/90 px-2 py-1 text-xs text-ink-muted shadow"
        >
          {pasteHint}
        </div>
      )}

      {/* "Tap to control" caption — shown when connected, canvas not focused, and
          the empty-state hint is not visible (only one bottom caption at a time). */}
      {state === 'connected' && !focused && !showEmpty && (
        <div className="pointer-events-none absolute inset-x-0 bottom-2 text-center text-xs text-ink-muted">
          Tap to control · pinch to zoom
        </div>
      )}

      {/* Blocking full-screen overlay for transitional/terminal states only */}
      {blockingOverlay && (
        <div className="absolute inset-0 flex flex-col items-center justify-center bg-canvas/80 p-6 text-center text-sm text-ink-muted">
          {blockingOverlay.copy}
        </div>
      )}

      {/* Non-blocking compact hint when connected but framebuffer is blank.
          Pinned to bottom-center, does NOT cover the canvas — pointer-events
          only on the buttons so users can click through the hint area.
          Dismissible (no per-session browser-open signal is available on the
          client); once dismissed stays hidden until remount. When dismissed,
          focus returns to the canvas so keyboard control is immediately available. */}
      {showEmpty && (
        <div className="pointer-events-none absolute inset-x-0 bottom-8 flex flex-col items-center gap-2">
          <p className="text-xs text-ink-muted drop-shadow">
            {openError ?? COPY.empty}
          </p>
          <div className="pointer-events-auto flex items-center gap-2">
            <button
              className={cn(
                'min-h-[44px]',
                'rounded bg-accent px-3 py-2 text-sm text-accent-ink shadow',
              )}
              onClick={openBrowser}
            >
              Open a browser
            </button>
            <button
              aria-label="Dismiss"
              className={cn(
                'min-h-[44px] min-w-[44px]',
                'rounded border border-edge bg-canvas/80 px-3 text-sm text-ink-muted shadow',
              )}
              onClick={dismissEmpty}
            >
              ✕
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
