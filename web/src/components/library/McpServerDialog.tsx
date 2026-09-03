import { useState, useRef } from 'react'
import { useSaveMcpServer, useDeleteMcpServer } from '@/api/hooks'
import type { LibraryMcpServer, McpServerConfig } from '@/api/types'
import { ApiRequestError } from '@/api/client'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Textarea } from '@/ui/Textarea'
import { Select } from '@/ui/Select'

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
    <Dialog open onClose={onClose} title={title} size="lg" initialFocus={firstFieldRef}>
      {/* Readonly: show config as pretty JSON */}
      {isReadonly && server && (
        <Textarea
          aria-label="Server config (JSON)"
          readOnly
          value={JSON.stringify(server.config, null, 2)}
          className="font-mono h-48"
        />
      )}

      {/* Editable form */}
      {!isReadonly && (
        <>
          {/* Name input (create mode only) */}
          {isCreate && (
            <Field
              label="Server name"
              htmlFor="mcp-name"
              error={newName.length > 0 && !nameIsValid ? 'Letters, numbers, . _ - only (must start with letter/digit).' : undefined}
            >
              <Input
                ref={(el) => { firstFieldRef.current = el }}
                id="mcp-name"
                type="text"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="my-mcp-server"
                aria-label="Server name"
                invalid={newName.length > 0 && !nameIsValid}
              />
            </Field>
          )}

          {/* Display server name when editing */}
          {!isCreate && (
            <p className="text-sm text-ink-muted font-medium">
              {server!.name}
            </p>
          )}

          {/* JSON toggle */}
          <div className="flex justify-end">
            <button
              type="button"
              onClick={jsonMode ? exitJsonMode : enterJsonMode}
              className="text-xs text-accent hover:underline"
            >
              {jsonMode ? 'Form mode' : 'Edit as JSON'}
            </button>
          </div>

          {jsonMode ? (
            <div className="space-y-1">
              <Textarea
                aria-label="Config JSON"
                value={jsonText}
                onChange={(e) => { setJsonText(e.target.value); setJsonError(null) }}
                invalid={!!jsonError}
                className="font-mono h-48"
              />
              {jsonError && (
                <p className="text-xs text-danger" role="alert">{jsonError}</p>
              )}
            </div>
          ) : (
            <>
              {/* Transport select */}
              <Field label="Transport" htmlFor="mcp-transport">
                <Select
                  ref={(el) => { if (!isCreate) firstFieldRef.current = el }}
                  id="mcp-transport"
                  value={transport}
                  onChange={(e) => { setTransport(e.target.value as Transport); setFormIsDirty(true) }}
                  aria-label="Transport"
                >
                  <option value="stdio">stdio</option>
                  <option value="http">http</option>
                  <option value="sse">sse</option>
                </Select>
              </Field>

              {transport === 'stdio' ? (
                <>
                  <Field label="Command" htmlFor="mcp-command">
                    <Input
                      id="mcp-command"
                      type="text"
                      value={command}
                      onChange={(e) => { setCommand(e.target.value); setFormIsDirty(true) }}
                      placeholder="npx"
                      aria-label="Command"
                    />
                  </Field>

                  <Field label="Args (one per line)" htmlFor="mcp-args">
                    <Textarea
                      id="mcp-args"
                      value={argsText}
                      onChange={(e) => { setArgsText(e.target.value); setFormIsDirty(true) }}
                      placeholder="-y&#10;some-mcp-package"
                      aria-label="Args"
                      className="font-mono h-24"
                    />
                  </Field>

                  <Field label="Env (KEY=VALUE per line)" htmlFor="mcp-env">
                    <Textarea
                      id="mcp-env"
                      value={envText}
                      onChange={(e) => { setEnvText(e.target.value); setFormIsDirty(true) }}
                      placeholder="API_KEY=secret"
                      aria-label="Env"
                      className="font-mono h-24"
                    />
                  </Field>
                </>
              ) : (
                <>
                  <Field label="URL" htmlFor="mcp-url">
                    <Input
                      id="mcp-url"
                      type="text"
                      value={url}
                      onChange={(e) => { setUrl(e.target.value); setFormIsDirty(true) }}
                      placeholder="https://example.com/mcp"
                      aria-label="URL"
                    />
                  </Field>

                  <Field label="Headers (Name: value per line)" htmlFor="mcp-headers">
                    <Textarea
                      id="mcp-headers"
                      value={headersText}
                      onChange={(e) => { setHeadersText(e.target.value); setFormIsDirty(true) }}
                      placeholder="Authorization: Bearer token"
                      aria-label="Headers"
                      className="font-mono h-24"
                    />
                  </Field>
                </>
              )}
            </>
          )}

          {/* API error */}
          {apiError && (
            <p className="text-xs text-danger" role="alert">{apiError}</p>
          )}
        </>
      )}

      {/* Footer */}
      <div className="flex gap-3 justify-end pt-2">
        <Button type="button" variant="secondary" onClick={onClose}>
          Cancel
        </Button>
        {!isCreate && !isReadonly && (
          <Button
            type="button"
            variant="danger"
            onClick={handleDelete}
            disabled={deleteMutation.isPending || saveMutation.isPending}
          >
            Delete
          </Button>
        )}
        {!isReadonly && (
          <Button
            type="button"
            size="lg"
            onClick={handleSave}
            disabled={!canSave}
            loading={saveMutation.isPending}
          >
            {saveMutation.isPending ? 'Saving…' : 'Save'}
          </Button>
        )}
      </div>
    </Dialog>
  )
}
