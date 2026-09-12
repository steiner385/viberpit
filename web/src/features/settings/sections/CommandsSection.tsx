import { useState } from 'react'
import { useLibraryCommands } from '@/api/hooks'
import type { LibraryCommand } from '@/api/types'
import { ItemEditorDialog } from './ItemEditorDialog'

export function CommandsSection() {
  const { data, isLoading } = useLibraryCommands()
  const [dialog, setDialog] = useState<{ id: string | null; plugin?: string; readonly?: boolean } | null>(null)

  if (isLoading) return <div className="text-sm opacity-70 p-4">Loading…</div>

  const commands = data?.commands ?? []
  const userCommands = commands.filter((c) => c.source === 'user')
  const pluginCommands = commands.filter((c) => c.source !== 'user')

  // Collect unique plugin ids in order
  const pluginIds = [...new Set(pluginCommands.map((c) => c.source))]

  function openCommand(c: LibraryCommand) {
    setDialog({ id: c.path, plugin: c.source !== 'user' ? c.source : undefined, readonly: c.readonly })
  }

  return (
    <div>
      {/* User commands */}
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">Your commands</h3>
        <button
          type="button"
          onClick={() => setDialog({ id: null })}
          className="text-xs px-2 py-1 rounded border border-edge hover:bg-gray-50 dark:hover:bg-gray-700"
        >
          New command
        </button>
      </div>
      <ul className="space-y-2 mb-6">
        {userCommands.map((c) => (
          <CommandRow key={c.path} command={c} onClick={() => openCommand(c)} />
        ))}
        {userCommands.length === 0 && (
          <li className="text-sm opacity-60 p-3">No user commands yet.</li>
        )}
      </ul>

      {/* Plugin commands */}
      {pluginCommands.length > 0 && (
        <>
          <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">From plugins</h3>
          <ul className="space-y-2">
            {pluginIds.map((pluginId) =>
              pluginCommands
                .filter((c) => c.source === pluginId)
                .map((c) => (
                  <CommandRow
                    key={`${pluginId}/${c.path}`}
                    command={c}
                    pluginBadge={pluginId}
                    onClick={() => openCommand(c)}
                  />
                )),
            )}
          </ul>
        </>
      )}

      {/* Editor dialog */}
      {dialog !== null && (
        <ItemEditorDialog
          kind="commands"
          id={dialog.id}
          plugin={dialog.plugin}
          readonly={dialog.readonly}
          onClose={() => setDialog(null)}
        />
      )}
    </div>
  )
}

function CommandRow({
  command,
  pluginBadge,
  onClick,
}: {
  command: LibraryCommand
  pluginBadge?: string
  onClick: () => void
}) {
  const displayPath = `/${command.path.replace(/\//g, ':')}`

  return (
    <li className="border border-edge rounded-lg p-3">
      <button
        type="button"
        className="w-full text-left"
        aria-label={command.name || displayPath}
        onClick={onClick}
      >
        <div className="flex items-baseline gap-2 min-w-0">
          <span className="font-medium font-mono text-sm">{displayPath}</span>
          {pluginBadge && (
            <span className="text-xs px-1.5 py-0.5 rounded-full border border-edge opacity-70 shrink-0">
              {pluginBadge}
            </span>
          )}
          {command.parseError && (
            <span className="text-xs px-1.5 py-0.5 rounded-full bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400 shrink-0">
              parse error
            </span>
          )}
        </div>
        {command.description && (
          <p className="text-sm opacity-70 truncate">{command.description}</p>
        )}
      </button>
    </li>
  )
}
