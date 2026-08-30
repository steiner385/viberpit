import type { ImageRef } from '@/lib/toolContent'

interface Props {
  images: ImageRef[]
}

/** Thumbnails for image blocks in a tool result (Read of a PNG, MCP screenshots). */
export function ResultImages({ images }: Props) {
  if (images.length === 0) return null
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {images.map((img, i) => (
        <a key={`${img.url.slice(0, 64)}:${i}`} href={img.url} target="_blank" rel="noreferrer">
          <img
            src={img.url}
            alt={img.name || 'tool result image'}
            className="max-h-64 rounded border border-gray-200 dark:border-gray-700 md:max-h-96"
          />
        </a>
      ))}
    </div>
  )
}
