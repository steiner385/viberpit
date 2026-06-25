import { useState, useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { useSettings, useUpdateSettings, type AppSettings } from '@/api/hooks'
import { PERMISSION_MODES } from '@/session/permissionModes'
import { NotificationsToggle } from './NotificationsToggle'

interface SettingsPanelProps {
  onClose: () => void
}

export function SettingsPanel({ onClose }: SettingsPanelProps) {
  const { data: settings } = useSettings()
  // Remount the form when the loaded settings transition from absent → present
  // so it initializes from server values via useState, not a setState-in-effect
  // sync. Keying on load state (not content) preserves in-progress edits across
  // background refetches.
  return (
    <SettingsForm key={settings ? 'loaded' : 'loading'} settings={settings} onClose={onClose} />
  )
}

interface SettingsFormProps {
  settings: AppSettings | undefined
  onClose: () => void
}

function SettingsForm({ settings, onClose }: SettingsFormProps) {
  const updateSettings = useUpdateSettings()
  const dialogRef = useRef<HTMLDivElement>(null)

  const [permissionMode, setPermissionMode] = useState(
    settings?.defaultPermissionMode ?? 'interactive',
  )
  const [thinkingCollapsed, setThinkingCollapsed] = useState(
    settings?.thinkingCollapsed ?? false,
  )
  const [repoRoot, setRepoRoot] = useState(settings?.repoRoot ?? '')
  const [toolDetail, setToolDetail] = useState<AppSettings['defaultToolDetail']>(
    settings?.defaultToolDetail ?? 'auto',
  )
  const [prAutofixMode, setPrAutofixMode] = useState(
    settings?.prAutofixMode ?? 'propose',
  )

  // Escape to close
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        onClose()
        return
      }
      // Focus trap
      if (e.key === 'Tab') {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
          'button, input, select, textarea, [tabindex]:not([tabindex="-1"])',
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

  function handleSave() {
    updateSettings.mutate({
      defaultPermissionMode: permissionMode,
      thinkingCollapsed,
      repoRoot: repoRoot.trim() || null,
      defaultToolDetail: toolDetail,
      prAutofixMode,
    }, {
      onSuccess: () => onClose(),
    })
  }

  const inputClass = cn(
    'w-full rounded-md border border-gray-300 dark:border-gray-600',
    'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
    'px-3 py-2 text-sm min-h-[44px]',
    'focus:outline-none focus:ring-2 focus:ring-blue-500',
    'placeholder:text-gray-400 dark:placeholder:text-gray-500',
  )

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-4 bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="settings-panel-title"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        ref={dialogRef}
        className={cn(
          'w-full max-w-md rounded-xl',
          'bg-white dark:bg-gray-800',
          'border border-gray-200 dark:border-gray-600',
          'shadow-xl p-6 space-y-4',
          'max-h-[90vh] overflow-y-auto',
        )}
      >
        <h2
          id="settings-panel-title"
          className="text-lg font-semibold text-gray-900 dark:text-gray-100"
        >
          Settings
        </h2>

        <div className="space-y-4">
          {/* Default tool approvals */}
          <div className="space-y-1">
            <label
              htmlFor="settings-permission-mode"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300"
            >
              Default tool approvals
            </label>
            <select
              id="settings-permission-mode"
              value={permissionMode}
              onChange={(e) => setPermissionMode(e.target.value)}
              className={inputClass}
            >
              {PERMISSION_MODES.map(({ value, label }) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
            {permissionMode === 'full-auto' && (
              <p
                role="alert"
                className="text-sm text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-900/30 rounded-md px-3 py-2"
              >
                ⚠ Full auto lets sessions run shell/edits without prompting. Anyone who can reach this server can run code as you.
              </p>
            )}
          </div>

          {/* PR auto-fix */}
          <div className="space-y-1">
            <label
              htmlFor="settings-pr-autofix"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300"
            >
              PR auto-fix
            </label>
            <select
              id="settings-pr-autofix"
              value={prAutofixMode}
              onChange={(e) => setPrAutofixMode(e.target.value)}
              className={inputClass}
            >
              <option value="off">Off — notify only</option>
              <option value="propose">Propose — show a one-tap fix action</option>
              <option value="auto">Auto — fix failing PRs automatically</option>
            </select>
          </div>

          {/* Transcript display */}
          <div className="space-y-3 border-t border-gray-200 dark:border-gray-700 pt-4">
            <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
              Transcript display
            </h3>
            <div className="space-y-1">
              <label
                htmlFor="settings-tool-detail"
                className="block text-sm font-medium text-gray-700 dark:text-gray-300"
              >
                Tool calls default
              </label>
              <select
                id="settings-tool-detail"
                value={toolDetail}
                onChange={(e) => setToolDetail(e.target.value as AppSettings['defaultToolDetail'])}
                className={inputClass}
              >
                <option value="auto">Auto-fold long groups</option>
                <option value="all-open">Expand all</option>
                <option value="all-closed">Collapse all</option>
              </select>
            </div>
            {/* Collapse thinking by default */}
            <div className="flex items-center gap-3">
              <input
                id="settings-thinking-collapsed"
                type="checkbox"
                checked={thinkingCollapsed}
                onChange={(e) => setThinkingCollapsed(e.target.checked)}
                className={cn(
                  'h-4 w-4 rounded border-gray-300 dark:border-gray-600',
                  'text-blue-600 focus:ring-2 focus:ring-blue-500',
                )}
              />
              <label
                htmlFor="settings-thinking-collapsed"
                className="text-sm font-medium text-gray-700 dark:text-gray-300 cursor-pointer"
              >
                Collapse thinking by default
              </label>
            </div>
          </div>

          {/* Repo clone root */}
          <div className="space-y-1">
            <label
              htmlFor="settings-repo-root"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300"
            >
              Repo clone root <span className="text-gray-400">(optional)</span>
            </label>
            <input
              id="settings-repo-root"
              type="text"
              value={repoRoot}
              onChange={(e) => setRepoRoot(e.target.value)}
              placeholder="/home/user/repos"
              className={inputClass}
            />
          </div>

          {/* Notifications */}
          <div className="space-y-3 border-t border-gray-200 dark:border-gray-700 pt-4">
            <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
              Notifications
            </h3>
            <NotificationsToggle />
          </div>
        </div>

        <div className="flex gap-3 justify-end pt-2">
          <Button
            type="button"
            variant="outline"
            onClick={onClose}
            disabled={updateSettings.isPending}
          >
            Close
          </Button>
          <Button
            type="button"
            onClick={handleSave}
            disabled={updateSettings.isPending}
            className="min-h-[44px]"
          >
            {updateSettings.isPending ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </div>
    </div>
  )
}
