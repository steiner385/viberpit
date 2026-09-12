import { CheckCircle2, CircleDot, Circle } from 'lucide-react'

interface TodoItem { content: string; status: string }

interface Props { payload: Record<string, unknown> }

function StatusMarker({ status }: { status: string }) {
  if (status === 'completed') {
    return <CheckCircle2 size={14} aria-hidden="true" data-testid="todo-status-completed" />
  }
  if (status === 'in_progress') {
    return <CircleDot size={14} aria-hidden="true" data-testid="todo-status-in_progress" />
  }
  return <Circle size={14} aria-hidden="true" data-testid="todo-status-pending" />
}

export function TodoList({ payload }: Props) {
  if (!Array.isArray(payload.todos)) {
    return (
      <pre className="overflow-auto rounded bg-canvas p-2 text-xs font-mono text-ink">
        {JSON.stringify(payload, null, 2)}
      </pre>
    )
  }

  const todos = payload.todos as TodoItem[]

  return (
    <ul className="space-y-1 text-sm">
      {todos.map((todo, i) => (
        <li key={i} className="flex items-start gap-2">
          <span
            className={
              todo.status === 'completed'
                ? 'text-success'
                : todo.status === 'in_progress'
                  ? 'text-accent'
                  : 'text-ink-muted'
            }
          >
            <StatusMarker status={todo.status} />
          </span>
          <span className="text-ink">{String(todo.content ?? '')}</span>
        </li>
      ))}
    </ul>
  )
}
