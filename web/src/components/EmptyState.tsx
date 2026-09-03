import { Plus } from 'lucide-react'
import { Button } from '@/ui/Button'
import { EmptyState as Empty } from '@/ui/EmptyState'

interface EmptyStateProps {
  onNew: () => void
}

export function EmptyState({ onNew }: EmptyStateProps) {
  return (
    <Empty
      title="No sessions yet"
      action={
        <Button onClick={onNew} size="lg" iconLeft={<Plus size={16} aria-hidden="true" />}>
          New Session
        </Button>
      }
    />
  )
}
