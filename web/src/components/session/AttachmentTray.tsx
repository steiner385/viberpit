import type { TrayItem } from './useAttachments'

export function AttachmentTray({ items, onRemove }: { items: TrayItem[]; onRemove: (k: string) => void }) {
  if (!items.length) return null
  return (
    <div className="flex flex-wrap gap-2">
      {items.map((it) => (
        <div key={it.key} className="relative">
          <img src={it.previewUrl} alt={it.filename}
               className={`h-16 w-16 rounded object-cover border ${it.status === 'error' ? 'border-red-500 opacity-50' : 'border-gray-300 dark:border-gray-600'}`} />
          {it.status === 'uploading' && (
            <div className="absolute inset-0 flex items-center justify-center text-xs">…</div>
          )}
          <button type="button" aria-label={`Remove ${it.filename}`}
                  onClick={() => onRemove(it.key)}
                  className="absolute -right-1 -top-1 rounded-full bg-gray-800 text-white w-5 h-5 text-xs leading-none">
            ✕
          </button>
        </div>
      ))}
    </div>
  )
}
