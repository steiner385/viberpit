import { BrowserTab } from './BrowserTab'

/** In-page VNC viewer for the reauth browser, pointed at the backend-reported session key. */
export function ReauthBrowserPanel({ vncSession }: { vncSession: string }) {
  return (
    <div className="mt-2 h-[60vh] min-h-0 overflow-hidden rounded-md border">
      <BrowserTab sessionId={vncSession} statusState="working" />
    </div>
  )
}
