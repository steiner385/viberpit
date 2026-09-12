// web/src/features/sessions/composer/AttachmentChips.tsx
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { Attachment } from './useImageAttachments'

interface Props { attachments: Attachment[]; onRemove: (key: string) => void }

export function AttachmentChips({ attachments, onRemove }: Props) {
  if (attachments.length === 0) return null
  return (
    <div className="flex flex-wrap gap-2" data-testid="attachment-chips">
      {attachments.map((a) => (
        <div key={a.key} className={cn(
          'relative flex items-center gap-2 rounded-md border px-2 py-1 text-xs',
          a.error
            ? 'border-danger text-danger'
            : 'border-edge-strong text-ink-muted')}>
          {!a.error && (
            <img src={a.previewUrl} alt={a.name}
                 className="h-8 w-8 rounded object-cover" />
          )}
          <span className="max-w-[10rem] truncate">{a.error || a.name}</span>
          <button type="button" aria-label={`Remove ${a.name}`}
                  onClick={() => onRemove(a.key)}
                  className="flex min-h-target min-w-target items-center justify-center text-ink-muted hover:text-danger focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
            <X size={12} aria-hidden="true" />
          </button>
        </div>
      ))}
    </div>
  )
}
