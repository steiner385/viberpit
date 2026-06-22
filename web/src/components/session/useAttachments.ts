import { useCallback, useState } from 'react'
import { uploadAttachment } from '@/api/client'

const OK = ['image/png', 'image/jpeg', 'image/webp', 'image/gif']
const MAX_BYTES = 10 * 1024 * 1024
const MAX_COUNT = 5

export interface TrayItem {
  key: string; filename: string; previewUrl: string
  status: 'uploading' | 'done' | 'error'; id?: string
}

export function useAttachments() {
  const [items, setItems] = useState<TrayItem[]>([])
  const [error, setError] = useState<string | null>(null)

  const addFiles = useCallback(async (files: File[]) => {
    setError(null)
    const valid: File[] = []
    setItems((cur) => {
      let count = cur.length
      for (const f of files) {
        if (!OK.includes(f.type)) { setError(`Unsupported file: ${f.name}`); continue }
        if (f.size > MAX_BYTES) { setError(`${f.name} is larger than 10 MB`); continue }
        if (count >= MAX_COUNT) { setError(`At most ${MAX_COUNT} images per message`); break }
        count++; valid.push(f)
      }
      return cur
    })
    for (const f of valid) {
      const key = `${f.name}-${f.size}-${Math.random().toString(36).slice(2)}`
      setItems((cur) => [...cur, {
        key, filename: f.name, previewUrl: URL.createObjectURL(f), status: 'uploading',
      }])
      try {
        const d = await uploadAttachment(f)
        setItems((cur) => cur.map((it) =>
          it.key === key ? { ...it, status: 'done', id: d.id } : it))
      } catch {
        setItems((cur) => cur.map((it) =>
          it.key === key ? { ...it, status: 'error' } : it))
        setError(`Upload failed: ${f.name}`)
      }
    }
  }, [])

  const remove = useCallback((key: string) => {
    setItems((cur) => cur.filter((it) => it.key !== key))
  }, [])
  const clear = useCallback(() => setItems([]), [])

  const ids = items.filter((it) => it.status === 'done' && it.id).map((it) => it.id!) as string[]
  const uploading = items.some((it) => it.status === 'uploading')
  return { items, addFiles, remove, clear, ids, uploading, error }
}
