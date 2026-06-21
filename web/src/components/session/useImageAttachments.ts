// web/src/components/session/useImageAttachments.ts
import { useCallback, useState } from 'react'

export const ALLOWED = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']
export const MAX_BYTES = 5 * 1024 * 1024
export const MAX_IMAGES = 8

export type Attachment = {
  sha: string; mime: string; name: string; previewUrl: string; error?: string
}

let _tmpId = 0

export function useImageAttachments() {
  const [attachments, setAttachments] = useState<Attachment[]>([])

  const addFiles = useCallback(async (files: FileList | File[]) => {
    const list = Array.from(files)
    let accepted = attachments.length
    for (const file of list) {
      if (accepted >= MAX_IMAGES) break
      const previewUrl = URL.createObjectURL(file)
      if (!ALLOWED.includes(file.type)) {
        setAttachments((a) => [...a, {
          sha: `err-${_tmpId++}`, mime: file.type, name: file.name,
          previewUrl, error: 'Unsupported file type' }])
        accepted++
        continue
      }
      if (file.size > MAX_BYTES) {
        setAttachments((a) => [...a, {
          sha: `err-${_tmpId++}`, mime: file.type, name: file.name,
          previewUrl, error: 'Image too large (max 5 MB)' }])
        accepted++
        continue
      }
      try {
        const form = new FormData()
        form.append('file', file)
        const resp = await fetch('/api/uploads', { method: 'POST', body: form })
        if (!resp.ok) throw new Error('upload failed')
        const ref = await resp.json()
        setAttachments((a) => [...a, {
          sha: ref.sha, mime: ref.mime, name: ref.name || file.name, previewUrl }])
        accepted++
      } catch {
        setAttachments((a) => [...a, {
          sha: `err-${_tmpId++}`, mime: file.type, name: file.name,
          previewUrl, error: 'Upload failed' }])
        accepted++
      }
    }
  }, [attachments.length])

  const remove = useCallback((sha: string) => {
    setAttachments((a) => {
      const found = a.find((x) => x.sha === sha)
      if (found) URL.revokeObjectURL(found.previewUrl)
      return a.filter((x) => x.sha !== sha)
    })
  }, [])

  const clear = useCallback(() => {
    setAttachments((a) => { a.forEach((x) => URL.revokeObjectURL(x.previewUrl)); return [] })
  }, [])

  const refs = useCallback(
    () => attachments.filter((a) => !a.error)
      .map((a) => ({ sha: a.sha, mime: a.mime, name: a.name })),
    [attachments])

  return { attachments, addFiles, remove, clear, refs }
}
