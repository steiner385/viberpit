import { useState, useId } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'

interface Props { text: string }

/**
 * Reasoning trace, rendered Claude-Code style: a compact, dim, italic one-liner
 * (`✻ Thinking`) that expands to the full text on click — no heavy bordered box.
 */
export function ThinkingBlock({ text }: Props) {
  const [open, setOpen] = useState(false)
  const regionId = useId()

  return (
    <div className="my-0.5">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={regionId}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'flex w-full items-center gap-1.5 px-3 py-1 text-left text-xs italic',
          'text-gray-400 dark:text-gray-500 leading-tight',
          'hover:text-gray-600 dark:hover:text-gray-300',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400 rounded',
        )}
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span aria-hidden="true">✻</span>
        <span>Thinking</span>
      </button>
      <div id={regionId} hidden={!open} className="ml-4 border-l-2 border-gray-200 dark:border-gray-700 px-3 py-1">
        <pre className="whitespace-pre-wrap text-xs italic text-gray-500 dark:text-gray-400 overflow-auto">
          {text}
        </pre>
      </div>
    </div>
  )
}
