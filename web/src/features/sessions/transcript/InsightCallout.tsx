import { Star } from 'lucide-react'
import { Markdown } from './Markdown'

interface Props { text: string }

export function InsightCallout({ text }: Props) {
  return (
    <div className="my-3 rounded-r border-l-[3px] border-warning bg-warning-wash px-3.5 py-3">
      <div className="mb-1.5 flex items-center gap-1.5 text-sm font-semibold text-warning">
        <Star size={14} aria-hidden="true" data-testid="insight-star" /> Insight
      </div>
      <Markdown>{text}</Markdown>
    </div>
  )
}
