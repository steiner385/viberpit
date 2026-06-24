import { useSyncExternalStore } from 'react'
import { subscribe, getToasts, dismissToast, type ToastItem } from '@/lib/toast'
import { cn } from '@/lib/utils'

const variantClass: Record<ToastItem['variant'], string> = {
  info: 'bg-gray-800 text-gray-50 dark:bg-gray-700',
  success: 'bg-gray-800 text-gray-50 dark:bg-gray-700',
  error: 'bg-red-600 text-white',
}

export function ToastViewport() {
  const toasts = useSyncExternalStore(subscribe, getToasts, getToasts)
  if (toasts.length === 0) return null
  return (
    <div
      className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-[calc(100vw-2rem)]"
      role="region"
      aria-label="Notifications"
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          role="status"
          aria-live="polite"
          className={cn('flex items-center gap-3 rounded-md px-4 py-3 text-sm shadow-lg', variantClass[t.variant])}
        >
          <span className="flex-1">{t.message}</span>
          {t.action && (
            <button
              type="button"
              onClick={() => { t.action!.onClick(); dismissToast(t.id) }}
              className="font-medium underline underline-offset-2 hover:opacity-80"
            >
              {t.action.label}
            </button>
          )}
          <button
            type="button"
            aria-label="Dismiss notification"
            onClick={() => dismissToast(t.id)}
            className="opacity-70 hover:opacity-100"
          >
            <span aria-hidden="true">×</span>
          </button>
        </div>
      ))}
    </div>
  )
}
