interface TodoItem { content: string; status: string }

interface Props { payload: Record<string, unknown> }

function statusMarker(status: string): string {
  if (status === 'completed') return '✓'
  if (status === 'in_progress') return '◐'
  return '○'
}

export function TodoList({ payload }: Props) {
  if (!Array.isArray(payload.todos)) {
    return (
      <pre className="overflow-auto rounded bg-gray-50 dark:bg-gray-900 p-2 text-xs font-mono text-gray-800 dark:text-gray-200">
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
                ? 'text-green-600 dark:text-green-400'
                : todo.status === 'in_progress'
                  ? 'text-blue-600 dark:text-blue-400'
                  : 'text-gray-400 dark:text-gray-500'
            }
          >
            {statusMarker(todo.status)}
          </span>
          <span className="text-gray-800 dark:text-gray-200">{String(todo.content ?? '')}</span>
        </li>
      ))}
    </ul>
  )
}
