import { useState, useRef } from 'react'
import {
  useLibraryItemContent,
  useSaveLibraryItem,
  useDeleteLibraryItem,
} from '@/api/hooks'
import type { LibraryItemKind } from '@/api/hooks'
import { ApiRequestError } from '@/api/client'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import { Field } from '@/ui/Field'
import { Input } from '@/ui/Input'
import { Textarea } from '@/ui/Textarea'

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
    <Dialog open onClose={onClose} title={title} size="lg" initialFocus={firstFieldRef}>
      {/* Name / path input in create mode */}
      {isCreate && (
        <Field
          label={nameLabel}
          htmlFor="item-editor-name"
          error={newName.length > 0 && !nameIsValid ? (
            kind === 'skills'
              ? 'Use lowercase letters, numbers, and hyphens only (start with letter/digit).'
              : 'Path segments: letters/numbers/._- (max 4, start with letter/digit).'
          ) : undefined}
        >
          <Input
            ref={(el) => { firstFieldRef.current = el }}
            id="item-editor-name"
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder={placeholder}
            aria-label={nameLabel}
            invalid={newName.length > 0 && !nameIsValid}
          />
        </Field>
      )}

      {/* Display item id when editing */}
      {!isCreate && (
        <p className="text-sm text-ink-muted">
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
        <div className="text-sm text-ink-muted">Loading…</div>
      )}
      <Textarea
        ref={(el) => { if (!isCreate) firstFieldRef.current = el }}
        aria-label="Content"
        value={content}
        onChange={(e) => handleContentChange(e.target.value)}
        readOnly={isReadonly}
        disabled={contentLoading}
        className="font-mono h-72"
      />

      {/* Save/delete error */}
      {saveError && (
        <p className="text-xs text-danger" role="alert">{saveError}</p>
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
