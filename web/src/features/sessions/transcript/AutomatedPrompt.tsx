import { Bot } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { ImageRef } from '@/lib/toolContent'
import type { PromptOrigin } from '@/session/events'
import { originLabel, originTitle } from '@/session/promptOrigin'

interface Props { origin: PromptOrigin; text?: string; images?: ImageRef[] }

/**
 * A prompt that entered the session without anyone typing it — an autopilot nudge, the
 * completion-readiness check, the AI assistant drawer.
 *
 * Deliberately NOT a variant of `UserBubble`. Right-aligned and accent-washed is a
 * promise that the reader wrote the words, and re-tinting that shape would have left
 * the same glanceable claim in a different colour. This leaves the lane entirely:
 * full-width, left-aligned, captioned with who sent it. What survives from `UserBubble`
 * is `whitespace-pre-wrap` — an automated prompt is plain text, never markdown, exactly
 * as a typed one is.
 *
 * The `Bot` glyph and the `info` tone are the same automation vocabulary the session
 * list's provenance marker uses (`SessionCard`), so the two surfaces teach each other.
 */
export function AutomatedPrompt({ origin, text, images }: Props) {
  return (
    <div
      data-testid="automated-prompt"
      data-origin-kind={origin.kind}
      className={cn(
        'rounded-lg border border-edge border-l-2 border-l-info',
        'bg-info-wash px-4 py-2',
      )}
    >
      <div
        className="mb-1 flex items-center gap-1 text-2xs font-medium text-info"
        title={originTitle(origin)}
      >
        <Bot size={11} aria-hidden="true" />
        <span>{originLabel(origin)}</span>
        {origin.inferred && (
          // Honest about the backfill: this origin was matched from automation records
          // after the fact, not recorded when the prompt was sent.
          <span className="font-normal text-ink-muted">· reconstructed</span>
        )}
      </div>
      <div className="whitespace-pre-wrap text-sm text-ink">{text}</div>
      {images && images.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {images.map((img, i) => (
            <a key={`${img.url}:${i}`} href={img.url} target="_blank" rel="noreferrer">
              <img src={img.url} alt={img.name} className="max-h-40 rounded border border-edge" />
            </a>
          ))}
        </div>
      )}
    </div>
  )
}
