import { useState, useRef, useEffect } from 'react'
import {
  useLibraryItemContent,
  useSaveLibraryItem,
  useDeleteLibraryItem,
} from '@/api/hooks'
import type { LibraryItemKind } from '@/api/hooks'
import { ApiRequestError } from '@/api/client'

interface ItemEditorDialogProps {
  kind: LibraryItemKind
  /** null = create-new mode (dialog asks for a name/path) */
  id: string | null
  /** plugin id when viewing a read-only plugin item */
  plugin?: string
  readonly?: boolean
  /** Pre-seed content in create mode (e.g. from AI draft). Ignored in edit mode. */
  initialContent?: string
  onClose: () => void
}

// --- Validation helpers ---

/** Skills: lowercase slug segments joined with hyphens, max 64 chars */
function isValidSkillName(name: string): boolean {
  return /^[a-z0-9][a-z0-9-]{0,63}$/.test(name)
}

/**
 * Commands: slug segments joined by '/'; each segment `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`;
 * max 4 segments; no segment may end in '.md'.
 */
function isValidCommandPath(path: string): boolean {
  const segments = path.split('/')
  if (segments.length < 1 || segments.length > 4) return false
  return segments.every(
    (seg) => /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(seg) && !seg.endsWith('.md'),
  )
}

function isValidId(kind: LibraryItemKind, id: string): boolean {
  return kind === 'skills' ? isValidSkillName(id) : isValidCommandPath(id)
}

/** Starter template for create mode */
function starterTemplate(kind: LibraryItemKind, name: string): string {
  if (kind === 'skills') {
    return `---\nname: ${name}\ndescription: \n---\n\n`
  }
  return `---\ndescription: \n---\n\n`
}

// --- Component ---

export function ItemEditorDialog({ kind, id, plugin, readonly = false, initialContent, onClose }: ItemEditorDialogProps) {
  const isCreate = id === null
  const dialogRef = useRef<HTMLDivElement>(null)
  const firstFieldRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null)

  // Create-mode name state
  const [newName, setNewName] = useState('')

  // Content state (textarea).
  // For create mode: initialize with initialContent if provided, else starter template.
  // For edit mode: derive from query data until the user edits (isDirty state).
  const [editedContent, setEditedContent] = useState(() => isCreate ? (initialContent ?? starterTemplate(kind, '')) : '')
  const [isDirty, setIsDirty] = useState(false)

  // Error state for save/delete mutations
  const [saveError, setSaveError] = useState<string | null>(null)

  // Hooks
  const contentQuery = useLibraryItemContent(kind, id ?? '', plugin)
  const saveMutation = useSaveLibraryItem(kind)
  const deleteMutation = useDeleteLibraryItem(kind)

  // Derive the content shown in the textarea:
  // - create mode: use editedContent (initialized to template)
  // - edit mode: use server data until the user edits, then switch to editedContent
  const content = isCreate || isDirty
    ? editedContent
    : (contentQuery.data?.content ?? '')

  function handleContentChange(val: string) {
    if (!isDirty) setIsDirty(true)
    setEditedContent(val)
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
          'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
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

  const effectiveId = isCreate ? newName : id!
  const nameIsValid = !isCreate || isValidId(kind, newName)
  // Also gate on: content loaded (edit mode), and no in-flight delete
  const contentLoading = !isCreate && contentQuery.data === undefined
  const canSave = !saveMutation.isPending && !deleteMutation.isPending && nameIsValid && effectiveId.length > 0 && !contentLoading

  const isReadonly = readonly || contentQuery.data?.readonly === true

  function extractErrorMessage(err: unknown): string {
    if (err instanceof ApiRequestError && typeof err.body === 'object' && err.body !== null && 'detail' in err.body) {
      const detail = (err.body as Record<string, unknown>)['detail']
      return typeof detail === 'string' ? detail : err.message
    }
    return err instanceof Error ? err.message : String(err)
  }

  function handleSave() {
    if (!canSave) return
    setSaveError(null)
    saveMutation.mutate(
      { id: effectiveId, content },
      {
        onSuccess: () => onClose(),
        onError: (err) => setSaveError(extractErrorMessage(err)),
      },
    )
  }

  function handleDelete() {
    if (!window.confirm(`Delete "${effectiveId}"?`)) return
    setSaveError(null)
    deleteMutation.mutate(
      { id: effectiveId },
      {
        onSuccess: () => onClose(),
        onError: (err) => setSaveError(extractErrorMessage(err)),
      },
    )
  }

  // Title
  let title: string
  if (isCreate) {
    title = kind === 'skills' ? 'New skill' : 'New command'
  } else if (isReadonly) {
    title = 'View (read-only)'
  } else {
    title = kind === 'skills' ? 'Edit skill' : 'Edit command'
  }

  const placeholder = kind === 'skills' ? 'my-skill' : 'project/my-command'
  const nameLabel = kind === 'skills' ? 'Skill name' : 'Command path'

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4 bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="item-editor-dialog-title"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        ref={dialogRef}
        className="w-full max-w-lg rounded-xl bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-600 shadow-xl p-6 space-y-4"
      >
        <h2
          id="item-editor-dialog-title"
          className="text-lg font-semibold text-gray-900 dark:text-gray-100"
        >
          {title}
        </h2>

        {/* Name / path input in create mode */}
        {isCreate && (
          <div className="space-y-1">
            <label
              htmlFor="item-editor-name"
              className="block text-sm text-gray-700 dark:text-gray-300"
            >
              {nameLabel}
            </label>
            <input
              ref={(el) => { firstFieldRef.current = el }}
              id="item-editor-name"
              type="text"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder={placeholder}
              aria-label={nameLabel}
              className="w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 px-2 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            {newName.length > 0 && !nameIsValid && (
              <p className="text-xs text-red-600 dark:text-red-400" role="alert">
                {kind === 'skills'
                  ? 'Use lowercase letters, numbers, and hyphens only (start with letter/digit).'
                  : 'Path segments: letters/numbers/._- (max 4, start with letter/digit).'}
              </p>
            )}
          </div>
        )}

        {/* Display item id when editing */}
        {!isCreate && (
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {kind === 'skills' ? id : `/${(id ?? '').replace(/\//g, ':')}`}
            {plugin && (
              <span className="ml-2 text-xs px-1.5 py-0.5 rounded-full border border-edge opacity-70">
                {plugin}
              </span>
            )}
          </p>
        )}

        {/* Content textarea */}
        {contentQuery.isLoading && !isCreate && (
          <div className="text-sm opacity-70">Loading…</div>
        )}
        <textarea
          ref={(el) => { if (!isCreate) firstFieldRef.current = el }}
          aria-label="Content"
          value={content}
          onChange={(e) => handleContentChange(e.target.value)}
          readOnly={isReadonly}
          disabled={contentLoading}
          className="font-mono text-sm w-full h-72 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 px-2 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-y disabled:opacity-60"
        />

        {/* Save/delete error */}
        {saveError && (
          <p className="text-xs text-red-600 dark:text-red-400" role="alert">{saveError}</p>
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
