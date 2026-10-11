export class ApiRequestError extends Error {
  readonly status: number
  readonly body: Record<string, unknown>

  constructor(status: number, body: Record<string, unknown>) {
    super(`API error ${status}`)
    this.name = 'ApiRequestError'
    this.status = status
    this.body = body
  }
}

/**
 * The machine-readable `code` an API error carries, or null.
 *
 * THE one decoder for this question — `ApiRequestError` is defined here, so the
 * shape of its body is this module's to know. The server spells the code three ways
 * (`error.code` on domain errors, `detail.code` on FastAPI's, and a top-level `code`
 * beside a string `detail` on the app-level exception handlers) and the create-session
 * endpoint uses the key `error` where others use `code`, so all are tried.
 *
 * Accepts either the thrown `ApiRequestError` or a raw body, because
 * `createSessionFailure.ts` decodes a body it already destructured.
 */
export function apiErrorCode(err: unknown): string | null {
  const body = err instanceof ApiRequestError ? err.body : err
  if (typeof body !== 'object' || body === null) return null
  const record = body as Record<string, unknown>
  for (const key of ['error', 'detail']) {
    const nested = record[key]
    if (typeof nested !== 'object' || nested === null) continue
    for (const field of ['code', 'error']) {
      const value = (nested as Record<string, unknown>)[field]
      if (typeof value === 'string') return value
    }
  }
  if (typeof record.code === 'string') return record.code
  // Flat `{"error": "<code>"}` (e.g. the MCP test endpoint).
  return typeof record.error === 'string' ? record.error : null
}

/** The `{detail: {message, reasons}}` a rule endpoint returns on failure; the server owns the copy. */
export interface ApiErrorDetail { message?: string; reasons?: string[] }

export function apiErrorDetail(err: unknown): ApiErrorDetail {
  const d = err instanceof ApiRequestError ? (err.body as { detail?: unknown } | null)?.detail : undefined
  if (typeof d !== 'object' || d === null) return {}
  const { message, reasons } = d as Record<string, unknown>
  return {
    message: typeof message === 'string' ? message : undefined,
    reasons: Array.isArray(reasons) ? (reasons as string[]) : undefined,
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  if (res.status === 204) return undefined as T
  const data = await res.json() as Record<string, unknown>
  if (!res.ok) {
    throw new ApiRequestError(res.status, data)
  }
  return data as T
}

export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, body),
  patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
}
