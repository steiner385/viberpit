/**
 * GeneralSection — the app-wide settings form, inline in the Settings tab
 * (formerly the SettingsPanel modal).
 */
import { useState } from 'react'
import { cn } from '@/lib/utils'
import { Button } from '@/components/ui/button'
import { useSettings, useUpdateSettings, type AppSettings } from '@/api/hooks'
import { PERMISSION_MODES } from '@/session/permissionModes'
import { NotificationsToggle } from './NotificationsToggle'
import { toast } from 'sonner'

export function GeneralSection() {
  const { data: settings } = useSettings()
  // Remount the form when settings transition absent → present so it
  // initializes from server values via useState (not setState-in-effect).
  return <GeneralForm key={settings ? 'loaded' : 'loading'} settings={settings} />
}

function GeneralForm({ settings }: { settings: AppSettings | undefined }) {
  const updateSettings = useUpdateSettings()
  const [permissionMode, setPermissionMode] = useState(settings?.defaultPermissionMode ?? 'interactive')
  const [thinkingCollapsed, setThinkingCollapsed] = useState(settings?.thinkingCollapsed ?? false)
  const [repoRoot, setRepoRoot] = useState(settings?.repoRoot ?? '')
  const [archiveHome, setArchiveHome] = useState(settings?.archiveHome ?? '')
  const [toolDetail, setToolDetail] = useState<AppSettings['defaultToolDetail']>(settings?.defaultToolDetail ?? 'auto')
  // Empty string = unlimited (sent to the backend as 0, which it maps to null).
  const [maxArchived, setMaxArchived] = useState(
    settings?.maxArchivedSessions != null ? String(settings.maxArchivedSessions) : '',
  )
  const [maxActive, setMaxActive] = useState(
    settings?.maxActiveSessions != null ? String(settings.maxActiveSessions) : '',
  )
  // Defaults to true — the server surfaces its NULL "unset" state as enabled.
  const [autoArchiveEnabled, setAutoArchiveEnabled] = useState(settings?.autoArchiveEnabled ?? true)
  // '' = server default (require_approval for fable).
  const [modelApprovalAction, setModelApprovalAction] = useState(settings?.modelApprovalAction ?? '')
  // Comma-separated list; empty = server default (fable).
  const [modelApprovalDenylist, setModelApprovalDenylist] = useState(settings?.modelApprovalDenylist ?? '')

  function handleSave() {
    const parsed = Number.parseInt(maxArchived, 10)
    const parsedActive = Number.parseInt(maxActive, 10)
    updateSettings.mutate({
      defaultPermissionMode: permissionMode,
      thinkingCollapsed,
      repoRoot: repoRoot.trim() || null,
      defaultToolDetail: toolDetail,
      // Blank / non-positive → 0 = "clear to unlimited". Backend clamps the ceiling.
      maxArchivedSessions: Number.isFinite(parsed) && parsed > 0 ? parsed : 0,
      // Send raw string: "" means "clear to server default", path means "use this path".
      archiveHome: archiveHome,
      maxActiveSessions: Number.isFinite(parsedActive) && parsedActive > 0 ? parsedActive : null,
      autoArchiveEnabled,
      // '' → null means "use server default". Non-empty value is sent as-is.
      modelApprovalAction: modelApprovalAction || null,
      modelApprovalDenylist: modelApprovalDenylist.trim() || null,
    }, { onSuccess: () => toast('Settings saved') })
  }

  const inputClass = cn(
    'w-full rounded-md border border-gray-300 dark:border-gray-600',
    'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100',
    'px-3 py-2 text-sm min-h-[44px]',
    'focus:outline-none focus:ring-2 focus:ring-blue-500',
    'placeholder:text-gray-400 dark:placeholder:text-gray-500',
  )

  return (
    <div className="space-y-4 max-w-md">
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

        {/* Model approval policy */}
        <div className="space-y-3 border-t border-gray-200 dark:border-gray-700 pt-4">
          <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
            Model approval
          </h3>
          <div className="space-y-1">
            <label
              htmlFor="settings-model-approval-action"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300"
            >
              Action for gated models
            </label>
            <select
              id="settings-model-approval-action"
              value={modelApprovalAction}
              onChange={(e) => setModelApprovalAction(e.target.value)}
              className={inputClass}
            >
              <option value="">Server default (require approval)</option>
              <option value="require_approval">Require approval</option>
              <option value="block">Block</option>
              <option value="allow">Allow</option>
            </select>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              What to do when a session requests a model on the approval denylist.
            </p>
          </div>
          <div className="space-y-1">
            <label
              htmlFor="settings-model-approval-denylist"
              className="block text-sm font-medium text-gray-700 dark:text-gray-300"
            >
              Approval denylist <span className="text-gray-400">(blank = default: fable)</span>
            </label>
            <input
              id="settings-model-approval-denylist"
              type="text"
              value={modelApprovalDenylist}
              onChange={(e) => setModelApprovalDenylist(e.target.value)}
              placeholder="fable"
              className={inputClass}
            />
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Comma-separated model ids or tier names that require approval.
            </p>
          </div>
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

        {/* Archive home */}
        <div className="space-y-1">
          <label
            htmlFor="settings-archive-home"
            className="block text-sm font-medium text-gray-700 dark:text-gray-300"
          >
            Archive storage path <span className="text-gray-400">(optional)</span>
          </label>
          <input
            id="settings-archive-home"
            type="text"
            value={archiveHome}
            onChange={(e) => setArchiveHome(e.target.value)}
            placeholder="/mnt/slow-drive/ccswitch-archives"
            className={inputClass}
          />
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Where archived session files are stored. Blank uses the server default.
            Existing archives are moved when this changes.
          </p>
        </div>

        {/* Auto-archive completed sessions */}
        <div className="space-y-1 border-t border-gray-200 dark:border-gray-700 pt-4">
          <div className="flex items-center gap-3">
            <input
              id="settings-auto-archive"
              type="checkbox"
              checked={autoArchiveEnabled}
              onChange={(e) => setAutoArchiveEnabled(e.target.checked)}
              className={cn(
                'h-4 w-4 rounded border-gray-300 dark:border-gray-600',
                'text-blue-600 focus:ring-2 focus:ring-blue-500',
              )}
            />
            <label
              htmlFor="settings-auto-archive"
              className="text-sm font-medium text-gray-700 dark:text-gray-300 cursor-pointer"
            >
              Auto-archive completed sessions
            </label>
          </div>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            When on, a session that finishes its goal is archived automatically. Turn this
            off to keep completed sessions on the dashboard until you archive them yourself.
          </p>
        </div>

        {/* Archived sessions */}
        <div className="space-y-1 border-t border-gray-200 dark:border-gray-700 pt-4">
          <label
            htmlFor="settings-max-archived"
            className="block text-sm font-medium text-gray-700 dark:text-gray-300"
          >
            Max archived sessions <span className="text-gray-400">(blank = unlimited)</span>
          </label>
          <input
            id="settings-max-archived"
            type="number"
            min={1}
            step={1}
            inputMode="numeric"
            value={maxArchived}
            onChange={(e) => setMaxArchived(e.target.value)}
            placeholder="Unlimited"
            className={inputClass}
          />
          <p className="text-xs text-gray-500 dark:text-gray-400">
            When the archived count exceeds this, the oldest are permanently purged.
          </p>
        </div>

        {/* Active sessions */}
        <div className="space-y-1 border-t border-gray-200 dark:border-gray-700 pt-4">
          <label
            htmlFor="settings-max-active"
            className="block text-sm font-medium text-gray-700 dark:text-gray-300"
          >
            Max active sessions <span className="text-gray-400">(blank = default (5))</span>
          </label>
          <input
            id="settings-max-active"
            type="number"
            min={1}
            step={1}
            inputMode="numeric"
            value={maxActive}
            onChange={(e) => setMaxActive(e.target.value)}
            placeholder="5 (default)"
            className={inputClass}
          />
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Sessions beyond this limit are queued and start automatically when a slot frees.
          </p>
        </div>

        {/* Notifications */}
        <div className="space-y-3 border-t border-gray-200 dark:border-gray-700 pt-4">
          <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
            Notifications
          </h3>
          <NotificationsToggle />
        </div>
      </div>

      <div className="flex justify-end pt-2">
        <Button type="button" onClick={handleSave} disabled={updateSettings.isPending} className="min-h-[44px]">
          {updateSettings.isPending ? 'Saving…' : 'Save'}
        </Button>
      </div>
    </div>
  )
}
