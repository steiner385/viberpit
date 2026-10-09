import { useId, useState } from 'react'
import { useDeleteMcpRepoRule, useGithubRepos, useMcpScope, useSetMcpDefault, useSetMcpRepoRule } from '@/api/hooks'
import { apiErrorCode } from '@/api/client'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { Button } from '@/ui/Button'
import { RadioGroup } from '@/ui/RadioGroup'
import { Select } from '@/ui/Select'

type Mode = 'on' | 'off'

/**
 * Per-server repo scope: the default ("On for all repos" / "Off by default") plus per-repo
 * exceptions. `serverKey` is the CLI/server key (`serverKey()` in mcpStatus.ts). Renders nothing
 * while the feature is switched off (scope GET answers 404 `mcp_scope_disabled`).
 */
export function McpScopeControl({ serverKey }: { serverKey: string }) {
  const uid = useId()
  const scope = useMcpScope()
  const repos = useGithubRepos()
  const setDefault = useSetMcpDefault()
  const setRule = useSetMcpRepoRule()
  const delRule = useDeleteMcpRepoRule()
  const [picked, setPicked] = useState('')
  const [pickedMode, setPickedMode] = useState<Mode | null>(null)

  if (scope.isError && apiErrorCode(scope.error) === 'mcp_scope_disabled') return null
  if (!scope.data) return null

  const defaultEnabled = scope.data.servers?.[serverKey]?.defaultEnabled ?? true
  const rules = (scope.data.rules ?? []).filter((r) => r.server === serverKey)
  // The server stores repos lower-cased; the picker offers only GitHub-listed repos without a rule.
  const taken = new Set(rules.map((r) => r.repo.toLowerCase()))
  const options = [...new Set((repos.data ?? []).map((r) => r.nameWithOwner.toLowerCase()))]
    .filter((r) => !taken.has(r))
    .sort()
  // An exception is by default the opposite of the server default.
  const mode: Mode = pickedMode ?? (defaultEnabled ? 'off' : 'on')
  const error = [setDefault, setRule, delRule].find((m) => m.isError)?.error
  const busy = setDefault.isPending || setRule.isPending || delRule.isPending

  return (
    <div className="mt-1 px-3 space-y-2">
      <div className="flex flex-wrap items-start gap-x-4 gap-y-1">
        <RadioGroup
          name={`mcp-scope-${uid}`}
          legend="Scope"
          orientation="horizontal"
          value={defaultEnabled ? 'on' : 'off'}
          options={[
            { value: 'on', label: 'On for all repos' },
            { value: 'off', label: 'Off by default' },
          ]}
          onChange={(v) => setDefault.mutate({ server: serverKey, defaultEnabled: v === 'on' })}
        />
        <p className="text-xs text-ink-muted self-center">Applies to new and resumed sessions.</p>
      </div>

      <div>
        <h4 className="text-xs font-medium text-ink">Exceptions</h4>
        {rules.length === 0 ? (
          <p className="text-xs text-ink-muted">None.</p>
        ) : (
          <ul className="space-y-1">
            {rules.map((r) => (
              <li key={r.repo} className="flex flex-wrap items-center gap-2 text-xs">
                <span className="break-all">{`${r.repo} — ${r.enabled ? 'on' : 'off'}`}</span>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  aria-label={`Remove exception ${r.repo}`}
                  disabled={busy}
                  onClick={() => delRule.mutate({ server: serverKey, repo: r.repo })}
                >
                  Remove
                </Button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-xs text-ink-muted min-w-[10rem] flex-1">
          Add exception
          <Select size="sm" value={picked} onChange={(e) => setPicked(e.target.value)}>
            <option value="">Select a repo…</option>
            {options.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </Select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-ink-muted">
          Exception state
          <Select size="sm" value={mode} onChange={(e) => setPickedMode(e.target.value as Mode)}>
            <option value="on">On</option>
            <option value="off">Off</option>
          </Select>
        </label>
        <Button
          type="button"
          variant="secondary"
          size="sm"
          disabled={!picked || busy}
          onClick={() =>
            setRule.mutate(
              { server: serverKey, repo: picked, enabled: mode === 'on' },
              { onSuccess: () => { setPicked(''); setPickedMode(null) } },
            )
          }
        >
          Add
        </Button>
      </div>

      {error != null && (
        <p className="text-xs text-danger break-words" role="alert">
          {extractMutationErrorMessage(error, 'Could not update scope.')}
        </p>
      )}
    </div>
  )
}
