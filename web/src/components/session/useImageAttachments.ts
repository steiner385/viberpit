// web/src/components/session/useImageAttachments.ts
import { useCallback, useState } from 'react'
import { maybeConvertHeic } from './heicConvert'

export const ALLOWED = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']
export const MAX_BYTES = 10 * 1024 * 1024
export const MAX_IMAGES = 5

// One attached image. `key` is a stable local handle for React + removal
// (dedup means two files can resolve to the same server `id`). `id` is the
// DB-backed attachment id returned by POST /api/attachments — the value sent
// in the dispatch/prompt `attachmentIds` payload.
export type Attachment = {
  key: string; id?: string; mime: string; name: string; previewUrl: string; error?: string
}

let _tmpId = 0
const _nextKey = () => `att-${_tmpId++}`

export function useImageAttachments(initial?: Attachment[]) {
  const [attachments, setAttachments] = useState<Attachment[]>(initial ?? [])

  const addFiles = useCallback(async (files: FileList | File[]) => {
    const list = Array.from(files)
    let accepted = attachments.length
    for (const original of list) {
      if (accepted >= MAX_IMAGES) break
      let file: File
      try {
        file = await maybeConvertHeic(original)
      } catch {
        setAttachments((a) => [...a, {
          key: _nextKey(), mime: original.type, name: original.name,
          previewUrl: '', error: 'Couldn’t read photo' }])
        accepted++
        continue
      }
      // Error chips never render a thumbnail (see AttachmentChips), so only a
      // successful upload allocates an object URL — no orphaned blob: URLs.
      if (!ALLOWED.includes(file.type)) {
        setAttachments((a) => [...a, {
          key: _nextKey(), mime: file.type, name: file.name,
          previewUrl: '', error: 'Unsupported file type' }])
        accepted++
        continue
      }
      if (file.size > MAX_BYTES) {
        setAttachments((a) => [...a, {
          key: _nextKey(), mime: file.type, name: file.name,
          previewUrl: '', error: 'Image too large (max 10 MB)' }])
        accepted++
        continue
      }
      const key = _nextKey()
      try {
        const form = new FormData()
        form.append('file', file)
        const resp = await fetch('/api/attachments', { method: 'POST', body: form })
        if (!resp.ok) throw new Error('upload failed')
        const ref = await resp.json()
        setAttachments((a) => [...a, {
          key, id: ref.id, mime: ref.mediaType || file.type,
          name: ref.filename || file.name, previewUrl: URL.createObjectURL(file) }])
        accepted++
      } catch {
        setAttachments((a) => [...a, {
          key, mime: file.type, name: file.name,
          previewUrl: '', error: 'Upload failed' }])
        accepted++
      }
    }
  }, [attachments.length])

  const remove = useCallback((key: string) => {
    setAttachments((a) => {
      const found = a.find((x) => x.key === key)
      if (found) URL.revokeObjectURL(found.previewUrl)
      return a.filter((x) => x.key !== key)
    })
  }, [])

  const clear = useCallback(() => {
    setAttachments((a) => { a.forEach((x) => URL.revokeObjectURL(x.previewUrl)); return [] })
  }, [])

  // The dispatch/prompt payload: the DB-backed attachment ids of successfully
  // uploaded (non-error) images.
  const refs = useCallback(
    (): string[] => attachments
      .filter((a) => !a.error && a.id)
      .map((a) => a.id as string),
    [attachments])

  return { attachments, addFiles, remove, clear, refs }
}
