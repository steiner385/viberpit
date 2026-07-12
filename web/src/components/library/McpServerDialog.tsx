import { useState, useRef, useEffect } from 'react'
import { useSaveMcpServer, useDeleteMcpServer } from '@/api/hooks'
import type { LibraryMcpServer, McpServerConfig } from '@/api/types'
import { ApiRequestError } from '@/api/client'

interface McpServerDialogProps {
  /** null = create-new mode */
  server: LibraryMcpServer | null
  onClose: () => void
}

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/

type Transport = 'stdio' | 'http' | 'sse'

/** Parse a Record<string,string> from key=value lines */
function parseKV(text: string): Record<string, string> {
  const result: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const idx = trimmed.indexOf('=')
    if (idx < 1) continue
    result[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1)
  }
  return result
}

/** Parse `Name: value` lines into a Record */
function parseHeaders(text: string): Record<string, string> {
  const result: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    const idx = trimmed.indexOf(':')
    if (idx < 1) continue
    result[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim()
  }
  return result
}

/** Serialize Record to `KEY=VALUE` lines */
function serializeKV(rec: Record<string, string> | undefined): string {
  if (!rec) return ''
  return Object.entries(rec).map(([k, v]) => `${k}=${v}`).join('\n')
}

/** Serialize Record to `Name: value` lines */
function serializeHeaders(rec: Record<string, string> | undefined): string {
  if (!rec) return ''
  return Object.entries(rec).map(([k, v]) => `${k}: ${v}`).join('\n')
}

/** Build McpServerConfig from form state */
function buildConfig(
  transport: Transport,
  command: string,
  argsText: string,
  envText: string,
  url: string,
  headersText: string,
): McpServerConfig {
  if (transport === 'stdio') {
    const args = argsText.split('\n').map((l) => l.trim()).filter(Boolean)
    const env = parseKV(envText)
    const cfg: McpServerConfig = { type: 'stdio', command }
    if (args.length > 0) cfg.args = args
    if (Object.keys(env).length > 0) cfg.env = env
    return cfg
  } else {
    const headers = parseHeaders(headersText)
    const cfg: McpServerConfig = { type: transport, url }
    if (Object.keys(headers).length > 0) cfg.headers = headers
    return cfg
  }
}

export function McpServerDialog({ server, onClose }: McpServerDialogProps) {
  const isCreate = server === null
  const isReadonly = !isCreate && server.readonly
  const dialogRef = useRef<HTMLDivElement>(null)
  const firstFieldRef = useRef<HTMLInputElement | HTMLSelectElement | null>(null)

  // Create-mode name
  const [newName, setNewName] = useState('')

  // Form state — initialized from server config or defaults
  const initialTransport: Transport = server?.transport ?? 'stdio'
  const [transport, setTransport] = useState<Transport>(initialTransport)
  const [command, setCommand] = useState(server?.config.command ?? '')
  const [argsText, setArgsText] = useState((server?.config.args ?? []).join('\n'))
  const [envText, setEnvText] = useState(serializeKV(server?.config.env))
  const [url, setUrl] = useState(
    server?.config.url ?? (server?.config as Record<string, unknown>)?.httpUrl as string ?? ''
  )
  const [headersText, setHeadersText] = useState(serializeHeaders(server?.config.headers))

  // Track whether the user has modified form fields (for pristine JSON mode)
  const [formIsDirty, setFormIsDirty] = useState(false)

  // JSON toggle mode
  const [jsonMode, setJsonMode] = useState(false)
  const [jsonText, setJsonText] = useState('')
  const [jsonError, setJsonError] = useState<string | null>(null)

  // Error from API
  const [apiError, setApiError] = useState<string | null>(null)

  const saveMutation = useSaveMcpServer()
  const deleteMutation = useDeleteMcpServer()

  // When entering JSON mode, use server.config for a pristine edit form
  function enterJsonMode() {
    if (!isCreate && server && !formIsDirty) {
      setJsonText(JSON.stringify(server.config, null, 2))
    } else {
      const cfg = buildConfig(transport, command, argsText, envText, url, headersText)
      setJsonText(JSON.stringify(cfg, null, 2))
    }
    setJsonError(null)
    setJsonMode(true)
  }

  function exitJsonMode() {
    setJsonError(null)
    setJsonMode(false)
  }

  // Focus first field on mount
  useEffect(() => {
    firstFieldRef.current?.focus()
  }, [])

  // Escape closes; Tab traps focus
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') { onClose(); return }
      if (e.key === 'Tab') {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
        )
        if (!focusable || focusable.length === 0) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (e.shiftKey) {
          if (document.activeElement === first) { e.preventDefault(); last.focus() }
        } else {
          if (document.activeElement === last) { e.preventDefault(); first.focus() }
        }
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  const effectiveName = isCreate ? newName : server!.name
  const nameIsValid = !isCreate || NAME_RE.test(newName)
  const canSave =
    !saveMutation.isPending && !deleteMutation.isPending && nameIsValid && effectiveName.length > 0

  let title: string
  if (isCreate) {
    title = 'Add MCP server'
  } else if (isReadonly) {
    title = 'MCP server (read-only)'
  } else {
    title = 'Edit MCP server'
  }

  function handleSave() {
    if (!canSave) return
    setApiError(null)

    let config: McpServerConfig
    if (jsonMode) {
      try {
        config = JSON.parse(jsonText) as McpServerConfig
      } catch {
        setJsonError('Invalid JSON')
        return
      }
    } else {
      const builtFields = buildConfig(transport, command, argsText, envText, url, headersText)
      if (!isCreate && server) {
        config = { ...server.config, ...builtFields }
        // Remove stale transport-specific keys after switching transport
        if (transport === 'stdio') {
          delete config.url
          delete config.httpUrl
          delete config.headers
        } else {
          delete config.command
          delete config.args
          delete config.env
        }
      } else {
        config = builtFields
      }
    }

    saveMutation.mutate(
      { name: effectiveName, config },
      {
        onSuccess: () => onClose(),
        onError: (err) => {
          if (err instanceof ApiRequestError) {
            const bodyDetail = err.body['detail']
            const msg =
              bodyDetail != null && typeof bodyDetail === 'object' && 'message' in bodyDetail
                ? String((bodyDetail as { message: unknown }).message)
                : err.message
            setApiError(msg)
          } else {
            setApiError((err as Error).message)
          }
        },
      },
    )
  }

  function handleDelete() {
    if (!window.confirm(`Delete "${effectiveName}"?`)) return
    deleteMutation.mutate({ name: effectiveName }, { onSuccess: () => onClose() })
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4 bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="mcp-dialog-title"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        ref={dialogRef}
        className="w-full max-w-lg rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-600 shadow-xl p-6 space-y-4"
      >
        <h2
          id="mcp-dialog-title"
          className="text-lg font-semibold text-gray-900 dark:text-gray-100"
        >
          {title}
        </h2>

        {/* Readonly: show config as pretty JSON */}
        {isReadonly && server && (
          <textarea
            aria-label="Server config (JSON)"
            readOnly
            value={JSON.stringify(server.config, null, 2)}
            className="font-mono text-sm w-full h-48 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 px-2 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-y"
          />
        )}

        {/* Editable form */}
        {!isReadonly && (
          <>
            {/* Name input (create mode only) */}
            {isCreate && (
              <div className="space-y-1">
                <label
                  htmlFor="mcp-name"
                  className="block text-sm text-gray-700 dark:text-gray-300"
                >
                  Server name
                </label>
                <input
                  ref={(el) => { firstFieldRef.current = el }}
                  id="mcp-name"
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="my-mcp-server"
                  aria-label="Server name"
                  className="w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 px-2 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                {newName.length > 0 && !nameIsValid && (
                  <p className="text-xs text-red-600 dark:text-red-400" role="alert">
                    Letters, numbers, . _ - only (must start with letter/digit).
                  </p>
                )}
              </div>
            )}

            {/* Display server name when editing */}
            {!isCreate && (
              <p className="text-sm text-gray-500 dark:text-gray-400 font-medium">
                {server!.name}
              </p>
            )}

            {/* JSON toggle */}
            <div className="flex justify-end">
              <button
                type="button"
                onClick={jsonMode ? exitJsonMode : enterJsonMode}
                className="text-xs text-blue-600 dark:text-blue-400 hover:underline"
              >
                {jsonMode ? 'Form mode' : 'Edit as JSON'}
              </button>
            </div>

            {jsonMode ? (
              <div className="space-y-1">
                <textarea
                  aria-label="Config JSON"
                  value={jsonText}
                  onChange={(e) => { setJsonText(e.target.value); setJsonError(null) }}
                  className="font-mono text-sm w-full h-48 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 px-2 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-y"
                />
                {jsonError && (
                  <p className="text-xs text-red-600 dark:text-red-400" role="alert">{jsonError}</p>
                )}
              </div>
            ) : (
              <>
                {/* Transport select */}
                <div className="space-y-1">
                  <label
                    htmlFor="mcp-transport"
                    className="block text-sm text-gray-700 dark:text-gray-300"
                  >
                    Transport
                  </label>
                  <select
                    ref={(el) => { if (!isCreate) firstFieldRef.current = el }}
                    id="mcp-transport"
                    value={transport}
                    onChange={(e) => { setTransport(e.target.value as Transport); setFormIsDirty(true) }}
                    aria-label="Transport"
                    className="w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 px-2 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  >
                    <option value="stdio">stdio</option>
                    <option value="http">http</option>
                    <option value="sse">sse</option>
                  </select>
                </div>

                {transport === 'stdio' ? (
                  <>
                    {/* Command */}
                    <div className="space-y-1">
                      <label
                        htmlFor="mcp-command"
                        className="block text-sm text-gray-700 dark:text-gray-300"
                      >
                        Command
                      </label>
                      <input
                        id="mcp-command"
                        type="text"
                        value={command}
                        onChange={(e) => { setCommand(e.target.value); setFormIsDirty(true) }}
                        placeholder="npx"
                        aria-label="Command"
                        className="w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 px-2 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                      />
                    </div>

                    {/* Args */}
                    <div className="space-y-1">
                      <label
                        htmlFor="mcp-args"
                        className="block text-sm text-gray-700 dark:text-gray-300"
                      >
                        Args <span className="opacity-60">(one per line)</span>
                      </label>
                      <textarea
                        id="mcp-args"
                        value={argsText}
                        onChange={(e) => { setArgsText(e.target.value); setFormIsDirty(true) }}
                        placeholder="-y&#10;some-mcp-package"
                        aria-label="Args"
                        className="font-mono text-sm w-full h-24 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 px-2 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-y"
                      />
                    </div>

                    {/* Env */}
                    <div className="space-y-1">
                      <label
                        htmlFor="mcp-env"
                        className="block text-sm text-gray-700 dark:text-gray-300"
                      >
                        Env <span className="opacity-60">(KEY=VALUE per line)</span>
                      </label>
                      <textarea
                        id="mcp-env"
                        value={envText}
                        onChange={(e) => { setEnvText(e.target.value); setFormIsDirty(true) }}
                        placeholder="API_KEY=secret"
                        aria-label="Env"
                        className="font-mono text-sm w-full h-24 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 px-2 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-y"
                      />
                    </div>
                  </>
                ) : (
                  <>
                    {/* URL */}
                    <div className="space-y-1">
                      <label
                        htmlFor="mcp-url"
                        className="block text-sm text-gray-700 dark:text-gray-300"
                      >
                        URL
                      </label>
                      <input
                        id="mcp-url"
                        type="text"
                        value={url}
                        onChange={(e) => { setUrl(e.target.value); setFormIsDirty(true) }}
                        placeholder="https://example.com/mcp"
                        aria-label="URL"
                        className="w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 px-2 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                      />
                    </div>

                    {/* Headers */}
                    <div className="space-y-1">
                      <label
                        htmlFor="mcp-headers"
                        className="block text-sm text-gray-700 dark:text-gray-300"
                      >
                        Headers <span className="opacity-60">(Name: value per line)</span>
                      </label>
                      <textarea
                        id="mcp-headers"
                        value={headersText}
                        onChange={(e) => { setHeadersText(e.target.value); setFormIsDirty(true) }}
                        placeholder="Authorization: Bearer token"
                        aria-label="Headers"
                        className="font-mono text-sm w-full h-24 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 px-2 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-y"
                      />
                    </div>
                  </>
                )}
              </>
            )}

            {/* API error */}
            {apiError && (
              <p className="text-xs text-red-600 dark:text-red-400" role="alert">{apiError}</p>
            )}
          </>
        )}

        {/* Footer */}
        <div className="flex gap-3 justify-end pt-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded border border-gray-300 dark:border-gray-600 px-3 py-2 text-sm hover:bg-gray-50 dark:hover:bg-gray-700"
          >
            Cancel
          </button>
          {!isCreate && !isReadonly && (
            <button
              type="button"
              onClick={handleDelete}
              disabled={deleteMutation.isPending || saveMutation.isPending}
              className="rounded border border-red-300 dark:border-red-700 text-red-600 dark:text-red-400 px-3 py-2 text-sm hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-60"
            >
              Delete
            </button>
          )}
          {!isReadonly && (
            <button
              type="button"
              onClick={handleSave}
              disabled={!canSave}
              className="rounded bg-blue-600 text-white px-3 py-2 text-sm hover:bg-blue-700 disabled:opacity-60"
            >
              {saveMutation.isPending ? 'Saving…' : 'Save'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
