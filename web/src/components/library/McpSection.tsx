import { useState } from 'react'
import { useLibraryMcp } from '@/api/hooks'
import type { LibraryMcpServer } from '@/api/types'
import { McpServerDialog } from './McpServerDialog'
import { McpAuthFlow } from './McpAuthFlow'

export function McpSection() {
  const { data, isLoading } = useLibraryMcp()
  const [dialogServer, setDialogServer] = useState<LibraryMcpServer | null | undefined>(undefined)

  if (isLoading) return <div className="text-sm opacity-70 p-4">Loading…</div>

  const servers = data?.servers ?? []

  return (
    <div>
      <p className="text-xs opacity-60 mb-3">
        MCP servers are shared across accounts; changes apply to newly started sessions.
      </p>
      <ul className="space-y-2">
        {servers.map((s) => (
          <li key={`${s.source}/${s.name}`}>
            <button
              className="w-full text-left border border-[var(--border)] rounded-lg p-3 hover:bg-gray-50 dark:hover:bg-gray-700/50 focus:outline-none focus:ring-2 focus:ring-blue-500"
              aria-label={s.name}
              onClick={() => setDialogServer(s)}
            >
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-medium">{s.name}</span>
                {/* Transport badge */}
                <span className="text-xs px-1.5 py-0.5 rounded-full border border-[var(--border)] opacity-70">
                  {s.transport}
                </span>
                {/* Source badge (plugin id only — omit for 'user') */}
                {s.source !== 'user' && (
                  <span className="text-xs px-1.5 py-0.5 rounded-full border border-[var(--border)] opacity-70">
                    {s.source}
                  </span>
                )}
                {/* Needs-auth badge */}
                {s.needsAuth.length > 0 && (
                  <span className="text-xs px-1.5 py-0.5 rounded-full bg-amber-100 dark:bg-amber-900/40 text-amber-700 dark:text-amber-300 border border-amber-300 dark:border-amber-700">
                    needs auth: {s.needsAuth.join(', ')}
                  </span>
                )}
              </div>
            </button>
            {/* Inline auth flow — rendered as sibling below the card button to avoid nesting interactive elements */}
            {s.needsAuth.length > 0 && (
              <McpAuthFlow server={s.name} accounts={s.needsAuth} />
            )}
          </li>
        ))}
        {servers.length === 0 && (
          <li className="text-sm opacity-70 p-4">No MCP servers configured.</li>
        )}
      </ul>

      <div className="mt-4">
        <button
          type="button"
          onClick={() => setDialogServer(null)}
          className="rounded border border-gray-300 dark:border-gray-600 px-3 py-2 text-sm hover:bg-gray-50 dark:hover:bg-gray-700"
        >
          Add server
        </button>
      </div>

      {/* dialogServer === undefined means no dialog open */}
      {dialogServer !== undefined && (
        <McpServerDialog
          server={dialogServer}
          onClose={() => setDialogServer(undefined)}
        />
      )}
    </div>
  )
}
