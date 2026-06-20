import { useState, useId } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/utils'

interface Props { text: string }

export function ThinkingBlock({ text }: Props) {
  const [open, setOpen] = useState(false)
  const regionId = useId()

  return (
    <div className="my-1 rounded border border-gray-200 dark:border-gray-700">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={regionId}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'flex w-full items-center gap-2 px-3 py-2 text-sm text-left',
          'text-gray-600 dark:text-gray-400',
          'hover:bg-gray-50 dark:hover:bg-gray-800/50',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-400',
          'min-h-[44px]',
        )}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span>Thinking</span>
      </button>
      <div
        id={regionId}
        hidden={!open}
        className="px-3 pb-3"
      >
        <pre className="whitespace-pre-wrap text-xs text-gray-700 dark:text-gray-300 overflow-auto">
          {text}
        </pre>
      </div>
    </div>
  )
}
