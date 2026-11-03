import { cn } from '@/lib/utils'
import type { ImageRef } from '@/lib/toolContent'
import { HighlightedText, type SearchHighlight } from './search/TranscriptSearchContext'

interface Props { text?: string; images?: ImageRef[]; highlight?: SearchHighlight }

export function UserBubble({ text, images, highlight }: Props) {
  return (
    <div className="flex justify-end">
      <div
        className={cn(
          'max-w-[80%] rounded-lg px-4 py-2',
          'bg-accent-wash',
          'text-ink',
          'whitespace-pre-wrap text-sm',
        )}
      >
        {text !== undefined && <HighlightedText text={text} highlight={highlight} />}
        {images && images.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            {images.map((img, i) => (
              <a key={`${img.url}:${i}`} href={img.url} target="_blank" rel="noreferrer">
                <img
                  src={img.url}
                  alt={img.name}
                  className="max-h-40 rounded border border-edge"
                />
              </a>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
