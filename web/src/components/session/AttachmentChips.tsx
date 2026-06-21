// web/src/components/session/AttachmentChips.tsx
import { cn } from '@/lib/utils'
import type { Attachment } from './useImageAttachments'

interface Props { attachments: Attachment[]; onRemove: (sha: string) => void }

export function AttachmentChips({ attachments, onRemove }: Props) {
  if (attachments.length === 0) return null
  return (
    <div className="flex flex-wrap gap-2" data-testid="attachment-chips">
      {attachments.map((a) => (
        <div key={a.sha} className={cn(
          'relative flex items-center gap-2 rounded-md border px-2 py-1 text-xs',
          a.error
            ? 'border-red-400 text-red-700 dark:text-red-300'
            : 'border-gray-300 dark:border-gray-600 text-gray-600 dark:text-gray-300')}>
          {!a.error && (
            <img src={a.previewUrl} alt={a.name}
                 className="h-8 w-8 rounded object-cover" />
          )}
          <span className="max-w-[10rem] truncate">{a.error || a.name}</span>
          <button type="button" aria-label={`Remove ${a.name}`}
                  onClick={() => onRemove(a.sha)}
                  className="flex min-h-[44px] min-w-[44px] items-center justify-center text-gray-500 hover:text-red-600 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400">
            ✕
          </button>
        </div>
      ))}
    </div>
  )
}
