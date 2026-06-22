import { cn } from '@/lib/utils'

interface ImageRef { url: string; mime: string; name: string }
interface Props { text?: string; images?: ImageRef[] }

export function UserBubble({ text, images }: Props) {
  return (
    <div className="flex justify-end">
      <div
        className={cn(
          'max-w-[80%] rounded-lg px-4 py-2',
          'bg-blue-50 dark:bg-blue-900/30',
          'text-gray-900 dark:text-gray-100',
          'whitespace-pre-wrap text-sm',
        )}
      >
        {text}
        {images && images.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            {images.map((img, i) => (
              <a key={`${img.url}:${i}`} href={img.url} target="_blank" rel="noreferrer">
                <img
                  src={img.url}
                  alt={img.name}
                  className="max-h-40 rounded border border-gray-200 dark:border-gray-700"
                />
              </a>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
