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
 * shape of its body is this module's to know. The server spells the code two ways
 * (`error.code` on domain errors, `detail.code` on FastAPI's) and the create-session
 * endpoint uses the key `error` where others use `code`, so all four are tried.
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
  return null
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
