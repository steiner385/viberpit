/**
 * NewSessionDialog — the `?dialog=new` create-session form.
 *
 * Lifted verbatim out of `components/Dashboard.tsx` (Task 17) so it survives the
 * shell that used to host it. Nothing below changed except the imports: the
 * field ids (`new-session-name`, `new-session-repo`, `new-session-account`, …)
 * are load-bearing for `AccountsNickname.test.tsx` and for anyone with the form
 * in muscle memory.
 */
import * as React from 'react'
import { useState, useRef } from 'react'
import { cn } from '@/lib/utils'
import { Dialog } from '@/ui/Dialog'
import { Button } from '@/ui/Button'
import { Input } from '@/ui/Input'
import { Select } from '@/ui/Select'
import { Textarea } from '@/ui/Textarea'
import { useModels, useGithubRepos, useGithubBranches, type CreateSessionInput } from '@/api/hooks'
import type { Account } from '@/api/types'
import { PERMISSION_MODES } from '@/session/permissionModes'
import { isGatedTier, modelName as modelDisplayName } from '@/session/modelName'

interface NewSessionDialogProps {
  accounts: Account[]
  onClose: () => void
  onSubmit: (input: CreateSessionInput) => void
  isPending: boolean
  externalError?: string | null
  defaultPermissionMode?: string
}

export function NewSessionDialog({ accounts, onClose, onSubmit, isPending, externalError, defaultPermissionMode }: NewSessionDialogProps) {
  const [name, setName] = useState('')
  const [account, setAccount] = useState('')
  const [initialPrompt, setInitialPrompt] = useState('')
  const [permissionMode, setPermissionMode] = useState(defaultPermissionMode ?? 'interactive')
  const [model, setModel] = useState('default')
  const [inlineError, setInlineError] = useState<string | null>(null)

  // Curated model list (server source of truth); "Default" = the account default.
  const modelsQuery = useModels()
  const models = modelsQuery.data ?? []
  const firstInputRef = useRef<HTMLInputElement>(null)

  // GitHub repo/branch picker state
  const [repoFilter, setRepoFilter] = useState('')
  const [selectedRepo, setSelectedRepo] = useState('')
  const [selectedBranch, setSelectedBranch] = useState('')

  const reposQuery = useGithubRepos()
  const branchesQuery = useGithubBranches(selectedRepo || undefined)

  const selectedRepoInfo = (reposQuery.data ?? []).find((r) => r.nameWithOwner === selectedRepo)

  const filteredRepos = (reposQuery.data ?? []).filter((r) =>
    r.nameWithOwner.toLowerCase().includes(repoFilter.trim().toLowerCase()),
  )

  // Effective branch, derived during render (no setState-in-effect): the user's
  // explicit pick wins; otherwise fall back to the loaded default branch, then
  // the repo's known default. Clearing the select (empty value) re-applies the
  // default, matching the previous effect-driven behavior.
  const effectiveBranch =
    selectedBranch || branchesQuery.data?.default || selectedRepoInfo?.defaultBranch || ''

  // Reset branch when repo changes.
  function handleRepoChange(nameWithOwner: string) {
    setSelectedRepo(nameWithOwner)
    setSelectedBranch('')
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setInlineError(null)
    onSubmit({
      name: name.trim() || undefined,
      account: account || undefined,
      initialPrompt: initialPrompt.trim() || undefined,
      repo: selectedRepo || undefined,
      branch: effectiveBranch || undefined,
      permissionMode,
      // "default" means account default — omit so the backend leaves the driver as-is.
      model: model && model !== 'default' ? model : undefined,
    })
  }

  return (
    <Dialog open onClose={onClose} title="New Session" size="md" initialFocus={firstInputRef}>
      <form onSubmit={handleSubmit} className="space-y-4">
        {/* Name */}
        <div className="space-y-1">
          <label htmlFor="new-session-name" className="block text-sm font-medium text-ink">
            Name <span className="text-ink-faint">(optional)</span>
          </label>
          <Input
            ref={firstInputRef}
            id="new-session-name"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="My session"
          />
        </div>

        {/* Launch target — the repo/branch to open and which account + model to run it with.
            Grouped so these "what am I launching" controls are discoverable together. */}
        <fieldset className="space-y-4 rounded-lg border border-edge p-4">
          <legend className="px-1 text-sm font-medium text-ink">
            Launch target
          </legend>

          {/* Repository filter */}
          <div className="space-y-1">
            <label htmlFor="new-session-repo-filter" className="block text-sm font-medium text-ink">
              Filter repos <span className="text-ink-faint">(optional)</span>
            </label>
            <Input
              id="new-session-repo-filter"
              type="text"
              value={repoFilter}
              onChange={(e) => setRepoFilter(e.target.value)}
              placeholder="owner/name…"
              autoComplete="off"
            />
          </div>

          {/* Repository */}
          <div className="space-y-1">
            <label htmlFor="new-session-repo" className="block text-sm font-medium text-ink">
              Repository <span className="text-ink-faint">(optional)</span>
            </label>
            <Select
              id="new-session-repo"
              value={selectedRepo}
              onChange={(e) => handleRepoChange(e.target.value)}
              disabled={reposQuery.isLoading}
              aria-busy={reposQuery.isLoading}
            >
              <option value="">
                {reposQuery.isLoading
                  ? 'Loading…'
                  : reposQuery.isError
                    ? '— could not load repos —'
                    : '— select repo —'}
              </option>
              {filteredRepos.map((repo) => (
                <option key={repo.nameWithOwner} value={repo.nameWithOwner}>
                  {repo.nameWithOwner}
                  {repo.isPrivate ? ' 🔒' : ''}
                  {repo.cloned ? ' · ✓ cloned' : ' · ↓ clones on start'}
                </option>
              ))}
            </Select>
            {selectedRepoInfo && (
              <p className="text-xs text-ink-faint">
                {selectedRepoInfo.isPrivate ? 'Private · ' : ''}
                {selectedRepoInfo.cloned
                  ? '✓ Already cloned into ~/GitHub/.ccswitch'
                  : '↓ Will be cloned into ~/GitHub/.ccswitch on first start (one-time; may take a moment for large repos)'}
              </p>
            )}
          </div>

          {/* Branch */}
          <div className="space-y-1">
            <label htmlFor="new-session-branch" className="block text-sm font-medium text-ink">
              Branch <span className="text-ink-faint">(optional)</span>
            </label>
            <Select
              id="new-session-branch"
              value={effectiveBranch}
              onChange={(e) => setSelectedBranch(e.target.value)}
              className={cn(!selectedRepo && 'opacity-50')}
              disabled={!selectedRepo || branchesQuery.isLoading}
              aria-busy={branchesQuery.isLoading}
            >
              <option value="">{branchesQuery.isLoading ? 'Loading…' : '— default —'}</option>
              {(branchesQuery.data?.branches ?? []).map((b) => (
                <option key={b} value={b}>{b}</option>
              ))}
            </Select>
            <p className="text-xs text-ink-faint">
              Clones and worktrees live under <code>~/GitHub/.ccswitch</code> — your real
              checkouts are never touched. Each session runs in its own isolated git
              worktree on the selected branch.
            </p>
          </div>

          {/* Account + Model — side-by-side on desktop, stacked on narrow mobile */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {/* Account */}
            <div className="space-y-1">
              <label htmlFor="new-session-account" className="block text-sm font-medium text-ink">
                Account <span className="text-ink-faint">(optional)</span>
              </label>
              <Select
                id="new-session-account"
                value={account}
                onChange={(e) => setAccount(e.target.value)}
              >
                <option value="">Auto-select</option>
                {accounts.map((a) => (
                  <option key={a.name} value={a.name}>{a.nickname || a.name}</option>
                ))}
              </Select>
            </div>

            {/* Model */}
            <div className="space-y-1">
              <label htmlFor="new-session-model" className="block text-sm font-medium text-ink">
                Model
              </label>
              <Select
                id="new-session-model"
                value={model}
                onChange={(e) => setModel(e.target.value)}
                disabled={modelsQuery.isLoading}
                aria-busy={modelsQuery.isLoading}
              >
                {/* Fallback so the "default" value always has a matching option before the list loads. */}
                {models.length === 0 && <option value="default">Default</option>}
                {models.map((m) => (
                  <option key={m.id} value={m.id}>{m.label}</option>
                ))}
              </Select>
              {isGatedTier(model) && (
                <p className="text-xs text-warning">
                  {modelDisplayName(model) || model} requires approval — this session will be held until approved.
                </p>
              )}
            </div>
          </div>
        </fieldset>

        {/* Tool approvals */}
        <div className="space-y-1">
          <label htmlFor="new-session-permission" className="block text-sm font-medium text-ink">
            Tool approvals
          </label>
          <Select
            id="new-session-permission"
            value={permissionMode}
            onChange={(e) => setPermissionMode(e.target.value)}
          >
            {PERMISSION_MODES.map(({ value, label }) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </Select>
        </div>

        {/* Initial prompt */}
        <div className="space-y-1">
          <label htmlFor="new-session-prompt" className="block text-sm font-medium text-ink">
            Initial prompt <span className="text-ink-faint">(optional)</span>
          </label>
          <Textarea
            id="new-session-prompt"
            value={initialPrompt}
            onChange={(e) => setInitialPrompt(e.target.value)}
            rows={3}
            placeholder="Start with…"
          />
        </div>

        {(inlineError || externalError) && (
          <p role="alert" className="text-sm text-danger">{inlineError ?? externalError}</p>
        )}

        <div className="flex gap-3 justify-end pt-2">
          <Button type="button" variant="secondary" onClick={onClose} disabled={isPending}>Cancel</Button>
          <Button type="submit" variant="primary" size="lg" loading={isPending}>
            {isPending ? 'Creating…' : 'Create'}
          </Button>
        </div>
      </form>
    </Dialog>
  )
}
