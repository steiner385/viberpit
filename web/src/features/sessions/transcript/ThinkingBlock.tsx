import { useState, useId } from 'react'
import { ChevronDown, ChevronRight, Sparkles } from 'lucide-react'
import { cn } from '@/lib/utils'

interface Props { text: string; defaultCollapsed: boolean }

/**
 * Reasoning trace, rendered Claude-Code style: a compact, dim, italic one-liner
 * (a sparkle glyph + `Thinking`) that expands to the full text on click — no heavy
 * bordered box.
 */
export function ThinkingBlock({ text, defaultCollapsed }: Props) {
  // Honor the persisted default until the user manually toggles, then stick.
  const [userToggled, setUserToggled] = useState<boolean | null>(null)
  const open = userToggled ?? !defaultCollapsed
  const regionId = useId()

  return (
    <div className="my-0.5">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={regionId}
        onClick={() => setUserToggled(!open)}
        className={cn(
          'flex w-full items-center gap-1.5 px-3 py-1 text-left text-xs italic',
          'text-ink-muted leading-tight',
          'hover:text-ink-muted',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent rounded',
        )}
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <Sparkles size={12} aria-hidden="true" />
        <span>Thinking</span>
      </button>
      <div id={regionId} hidden={!open} className="ml-4 border-l-2 border-edge px-3 py-1">
        <pre className="whitespace-pre-wrap text-xs italic text-ink-muted overflow-auto">
          {text}
        </pre>
      </div>
    </div>
  )
}
