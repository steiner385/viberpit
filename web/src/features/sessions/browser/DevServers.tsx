/**
 * The Browser tab's "Dev servers" section (D-18): the dev servers this session
 * started, each with an Open that goes to the VIEWER's display by the route the
 * server picked for this viewer — a link on this device (`local-link`), a link over
 * the tailnet (`tailnet-link`), or the session's virtual display (`vnc`).
 *
 * The UI never builds or edits a URL: a link opens exactly the server's `url`, and
 * a `vnc` Open posts only the detected port. Labels and titles are untrusted text
 * (a command line, a page's `<title>`) and render as text.
 */
import { useId, useState } from 'react'
import { ExternalLink, Monitor } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Button } from '@/ui/Button'
import { SectionHeading } from '@/ui/SectionHeading'
import { apiErrorCode, apiErrorDetail } from '@/api/client'
import { isSafeHttpUrl } from '@/lib/safeUrl'
import { useOpenDevServer } from '@/api/devServerHooks'
import type { DevServer } from '@/api/types'
import { DEV_SERVERS_COPY } from './devServersCopy'

/** Untrusted text is capped before it reaches the DOM, `title` attribute included. */
const TEXT_MAX = 160
const cap = (s: string) => (s.length > TEXT_MAX ? `${s.slice(0, TEXT_MAX)}…` : s)

interface Props {
  sessionId: string
  /** The detected servers — SessionBody's query, polled while this tab is shown. */
  servers: DevServer[]
  /** Re-poll now: an Open found its port gone. */
  refetch: () => void
  /** A line from the viewer, e.g. "No preview open" after its display went away. */
  notice: string | null
  /** A `vnc` Open landed: the viewer should show the session's display. */
  onPreviewOpened: () => void
  /** Fill the tab (no viewer below), instead of a capped strip above it. */
  fill?: boolean
}

export function DevServers({ sessionId, servers, refetch, notice, onPreviewOpened, fill = false }: Props) {
  const headingId = useId()
  const openPreview = useOpenDevServer(sessionId, refetch)
  const [error, setError] = useState<{ port: number; copy: string } | null>(null)

  function open(s: DevServer) {
    setError(null)
    if (s.open.kind === 'vnc') {
      openPreview.mutate(s.port, {
        onSuccess: onPreviewOpened,
        onError: (err: unknown) => {
          // `fetch` rejects with a TypeError only when the request never got an answer.
          const copy = err instanceof TypeError
            ? DEV_SERVERS_COPY.unreachable
            : DEV_SERVERS_COPY.errors[apiErrorCode(err) ?? ''] ?? apiErrorDetail(err).message ?? DEV_SERVERS_COPY.failed
          setError({ port: s.port, copy })
        },
      })
      return
    }
    // A link is opened only when it is plainly http(s) — never a `javascript:` URL.
    if (!isSafeHttpUrl(s.open.url)) {
      setError({ port: s.port, copy: DEV_SERVERS_COPY.badLink })
      return
    }
    window.open(s.open.url, '_blank', 'noopener,noreferrer')
  }

  return (
    <section
      aria-labelledby={headingId}
      className={cn('shrink-0 overflow-y-auto border-b border-edge px-3 py-2', fill ? 'flex-1' : 'max-h-[40%]')}
    >
      <SectionHeading id={headingId}>{DEV_SERVERS_COPY.heading}</SectionHeading>
      {notice && <p role="status" className="mt-1 text-xs text-ink-muted">{notice}</p>}
      {servers.length === 0 ? (
        <p className="mt-1 text-sm text-ink-muted">{DEV_SERVERS_COPY.empty}</p>
      ) : (
        <ul className="divide-y divide-edge">
          {servers.map((s) => {
            const link = s.open.kind !== 'vnc'
            const pending = openPreview.isPending && openPreview.variables === s.port
            return (
              <li key={s.port} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2">
                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 items-baseline gap-2">
                    <span className="truncate font-mono text-sm text-ink" title={cap(s.label)}>{cap(s.label)}</span>
                    <span className="shrink-0 font-mono text-xs text-ink-muted">:{s.port}</span>
                  </div>
                  {s.title && (
                    <p className="truncate text-xs text-ink-muted" title={cap(s.title)}>{cap(s.title)}</p>
                  )}
                  {s.bind === 'all' && <p className="text-xs text-ink-muted">{DEV_SERVERS_COPY.reachable}</p>}
                </div>
                <Button
                  size="sm"
                  variant="secondary"
                  className="min-h-target"
                  aria-label={`Open :${s.port}${link ? ' in a new tab' : " in the session's browser"}`}
                  iconLeft={link ? <ExternalLink size={14} aria-hidden="true" /> : <Monitor size={14} aria-hidden="true" />}
                  loading={pending}
                  // One Open at a time: the section has one error line per row and
                  // the server replaces the previous preview anyway.
                  disabled={openPreview.isPending && !pending}
                  onClick={() => open(s)}
                >
                  Open
                </Button>
                {error?.port === s.port && (
                  <p role="alert" className="basis-full text-xs text-danger">{error.copy}</p>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
