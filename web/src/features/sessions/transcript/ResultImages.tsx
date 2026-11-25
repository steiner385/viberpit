import { useState } from 'react'
import { isAttachmentUrl, type ImageRef } from '@/lib/toolContent'

interface Props {
  images: ImageRef[]
}

/** A transcript image's box — shared with `WorktreeImage`. */
export const RESULT_IMAGE_CLASS = 'h-auto max-h-64 max-w-full rounded border border-edge object-contain md:max-h-96'
/** What stands in for an image that cannot be shown. */
export const IMAGE_NOTICE_CLASS = 'block rounded border border-edge p-3 text-sm text-ink-muted'

/** Shared by tool output, artifacts and assistant Markdown; never fetch host paths. */
export function ResultImage({ url, name, linked = false }: Pick<ImageRef, 'url' | 'name'> & { linked?: boolean }) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  const attachment = isAttachmentUrl(url)
  const inline = /^data:image\/(?:png|jpeg|gif|webp);base64,[a-zA-Z0-9+/=\s]+$/.test(url)
  let remote = false
  try {
    const parsed = new URL(url)
    remote = ['https:', 'http:'].includes(parsed.protocol) && !parsed.username && !parsed.password
  } catch { /* Relative URLs are accepted only for canonical attachments. */ }
  const available = (attachment || inline || remote) && failedUrl !== url
  const label = name || 'tool result image'
  if (!available) {
    return <span className={IMAGE_NOTICE_CLASS}>Image unavailable{ name ? `: ${name}.` : '.' }</span>
  }
  const image = <img src={url} alt={label} onError={() => setFailedUrl(url)} className={RESULT_IMAGE_CLASS} />
  if (linked) return image
  return (
    <span className="block min-w-0 max-w-full">
      <a href={url} target="_blank" rel="noopener noreferrer" aria-label={`Open full size: ${label}`} className="block w-fit max-w-full rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">
        {image}
      </a>
      {attachment && <a href={url} download={name || 'image'} className="inline-flex min-h-target items-center text-xs font-medium text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent">Download image</a>}
    </span>
  )
}

/** Images stay in the transcript even when their tool's details are collapsed. */
export function ResultImages({ images }: Props) {
  if (images.length === 0) return null
  return (
    <div className="mt-2 flex min-w-0 flex-wrap gap-2">
      {images.map((img, i) => <ResultImage key={`${img.url.slice(0, 64)}:${i}`} url={img.url} name={img.name} />)}
    </div>
  )
}
