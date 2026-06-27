import { BrowserTab } from './BrowserTab'

/** In-page VNC viewer for the reauth browser, pointed at the backend-reported session key.
 *
 *  Large by default and user-resizable (drag the bottom-right handle): the virtual display
 *  is 1280×800, so a cramped box renders the Claude login as an unusable thumbnail. BrowserTab
 *  fills this container, auto-scales the framebuffer, and re-fits on resize (its ResizeObserver),
 *  so growing the box is all that's needed to make the login legible and clickable. */
export function ReauthBrowserPanel({ vncSession }: { vncSession: string }) {
  return (
    <div className="mt-2 h-[72vh] max-h-[88vh] min-h-[360px] w-full max-w-full resize overflow-hidden rounded-md border">
      <BrowserTab sessionId={vncSession} statusState="working" />
    </div>
  )
}
