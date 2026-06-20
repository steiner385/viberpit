import { cn } from '@/lib/utils'

interface Props { text: string }

export function UserBubble({ text }: Props) {
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
      </div>
    </div>
  )
}
