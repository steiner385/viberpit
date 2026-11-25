import { Bell } from 'lucide-react'

/** `PushNotification`: there is no terminal to ring here, so the notice is a line in the transcript. */
export function PushNotificationCard({ message }: { message: string }) {
  return (
    <p className="my-2 flex items-start gap-2 text-sm text-ink-muted" data-testid="push-notification-card">
      <Bell size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
      <span className="min-w-0 break-words">Notification: {message || '(no message)'}</span>
    </p>
  )
}
