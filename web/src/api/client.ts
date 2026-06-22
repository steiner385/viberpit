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

export interface AttachmentDescriptor {
  id: string; mediaType: string; filename: string; byteSize: number; url: string
}

export async function uploadAttachment(file: File): Promise<AttachmentDescriptor> {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch('/api/attachments', { method: 'POST', body: form })
  const data = await res.json() as Record<string, unknown>
  if (!res.ok) throw new ApiRequestError(res.status, data)
  return data as unknown as AttachmentDescriptor
}
