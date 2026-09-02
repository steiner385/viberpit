import { Plus } from 'lucide-react'
import { Button } from '@/ui/Button'

interface EmptyStateProps {
  onNew: () => void
}

export function EmptyState({ onNew }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-16 gap-4 text-center">
      <p className="text-gray-600 dark:text-gray-400 text-sm">
        No sessions yet
      </p>
      <Button onClick={onNew} size="lg" className="min-h-[44px]">
        <Plus size={16} aria-hidden="true" className="mr-2" />
        New Session
      </Button>
    </div>
  )
}
