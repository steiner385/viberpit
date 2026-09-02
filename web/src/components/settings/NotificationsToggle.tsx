import { useState } from 'react'
import { enablePush, disablePush, pushSupported } from '@/api/push'

export function NotificationsToggle() {
  const [state, setState] = useState<'idle' | 'subscribed' | 'denied'>('idle')
  const [busy, setBusy] = useState(false)

  if (!pushSupported()) {
    return (
      <p className="text-sm text-ink-muted">
        Push notifications are not supported in this browser (needs HTTPS + a modern browser).
      </p>
    )
  }

  async function onEnable() {
    setBusy(true)
    try {
      const r = await enablePush()
      setState(r === 'subscribed' ? 'subscribed' : r === 'denied' ? 'denied' : 'idle')
    } finally {
      setBusy(false)
    }
  }

  async function onDisable() {
    setBusy(true)
    try {
      await disablePush()
      setState('idle')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex items-center justify-between gap-3">
      <div>
        <div className="text-sm font-medium">Phone notifications</div>
        {state === 'denied' && (
          <div className="text-xs text-destructive">Permission denied — enable it in browser settings.</div>
        )}
        {state === 'subscribed' && (
          <div className="text-xs text-ink-muted">This device will receive alerts.</div>
        )}
      </div>
      {state === 'subscribed' ? (
        <button disabled={busy} onClick={onDisable} className="text-sm underline">
          Disable
        </button>
      ) : (
        <button disabled={busy} onClick={onEnable} className="text-sm underline">
          Enable
        </button>
      )}
    </div>
  )
}
