import { loginUrlFor, LOGIN_PATH } from '@/lib/loginRedirect'
import { displayGateBlocks } from './displayGate'

export class ApiRequestError extends Error {
  readonly status: number
  readonly body: Record<string, unknown>
  /** `Retry-After` in seconds (a 429), or null when absent or an HTTP-date. */
  readonly retryAfterS: number | null

  constructor(status: number, body: Record<string, unknown>, retryAfterS: number | null = null) {
    super(`API error ${status}`)
    this.name = 'ApiRequestError'
    this.status = status
    this.body = body
    this.retryAfterS = retryAfterS
  }
}

/** Only a 429 or 503 carries a meaningful Retry-After; nothing else is read. */
function retryAfterSeconds(res: Response): number | null {
  if (res.status !== 429 && res.status !== 503) return null
  const raw = res.headers.get('Retry-After')
  return raw !== null && /^\d+$/.test(raw.trim()) ? Number(raw.trim()) : null
}

/** Latched on the first passkey 401: the page is leaving, so a burst of failing
 *  queries must not each restart the navigation. */
let leavingForLogin = false

/**
 * A 401 whose body names the passkey login (`error.login === "passkey"`, sent
 * only when the server has passkeys enabled, #1410) sends the browser to
 * `/login?next=<here>`. A full navigation, not a router push: the shell and its
 * query cache must remount under the new session cookie. `replace`, so Back
 * does not return to a page that would only bounce here again. Any other 401
 * is left to the caller, as before.
 */
function redirectOnPasskey401(status: number, data: Record<string, unknown>): void {
  if (status !== 401 || leavingForLogin) return
  const error = data.error
  if (typeof error !== 'object' || error === null || (error as Record<string, unknown>).login !== 'passkey') return
  const { pathname, search, hash } = window.location
  if (pathname === LOGIN_PATH) return
  leavingForLogin = true
  window.location.replace(loginUrlFor(`${pathname}${search}${hash}`))
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
  // A display session's own 403 without the round trip (#3544): same error shape the
  // server would send, so callers and the query retry policy see an ordinary refusal.
  if (method === 'GET' && displayGateBlocks(path)) {
    throw new ApiRequestError(403, { error: { code: 'DISPLAY_SCOPE_FORBIDDEN', message: 'a display session may not call this route' } })
  }
  const res = await fetch(`/api${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  if (res.status === 204) return undefined as T
  const data = await res.json() as Record<string, unknown>
  if (!res.ok) {
    redirectOnPasskey401(res.status, data)
    throw new ApiRequestError(res.status, data, retryAfterSeconds(res))
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
