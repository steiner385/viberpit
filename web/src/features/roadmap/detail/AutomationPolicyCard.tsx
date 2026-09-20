// The product Overview's Automation card (product automation policy phase 1
// design §9): every editable `PRODUCT_POLICY_SCHEMA` field rendered through the
// SAME `ConfigInput` the automations Config tab uses, reading
// `project.automationPolicy` (the raw stored blob, or null when the product
// never opted in) against the server's defaults (Capacity-owned dials appear
// as one read-only line, deprecated ones not at all — spec 2026-09-20 D5/D6).
// Disabled — with an explanatory note — until the product is live: the
// onboarding wizard above it is what gets it there.
import { Link } from 'react-router-dom'
import { productPath } from '../roadmapNavState'
import type { ConfigField } from '@/api/automations'
import type { CapacityProduct } from '@/api/capacity'
import type { Project, ProductImplementationDecision } from '@/api/pm'
import type { ProductNode } from '@/api/roadmap'
import type { GateSpec } from '@/api/sdlc'
import { usePolicySchema, useUpdateProductPolicy, useProductPolicyCoverage, useProductImplementationPolicy, useSdlcGates, useCapacity } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { ConfigInput } from '@/features/delivery/automation/ConfigInput'
import { deliveryPath } from '@/features/delivery/deliveryNavState'
import { Banner } from '@/ui/Banner'
import { Field } from '@/ui/Field'
import { Skeleton } from '@/ui/Skeleton'

/** `dispatch` first — the issue-dispatch setting is the first
 *  thing a human should see, not buried among the other dials. */
function orderedFields(schema: ConfigField[]): ConfigField[] {
  const dispatch = schema.filter((f) => f.key === 'dispatch')
  const rest = schema.filter((f) => f.key !== 'dispatch')
  return [...dispatch, ...rest]
}

// Findability (spec 2026-09-12 D9, reshaped by 2026-09-20 D5): the dials group
// under three legends. *Dispatch* holds what this card OWNS (dispatch, reach,
// eligibility); rank, cap mode and pin are owned by Delivery → Capacity
// (`surface: 'capacity'`) and appear here as one read-only line; a deprecated
// field (D6) appears nowhere. A key this schema does not yet know about falls
// into Dispatch rather than vanishing.
const MODELS_FIELD_KEYS = new Set(['default_model', 'ladder_top', 'unattended_model_floor'])
const GATES_FIELD_KEYS = new Set([
  'risk_tolerance', 'triage_auto_accept', 'give_up_after_bounces', 'max_initiatives_in_flight',
])

interface FieldGroups {
  dispatch: ConfigField[]
  models: ConfigField[]
  gates: ConfigField[]
}

function groupFields(fields: ConfigField[]): FieldGroups {
  const groups: FieldGroups = { dispatch: [], models: [], gates: [] }
  for (const field of fields) {
    if (field.deprecated || field.surface === 'capacity') continue
    if (MODELS_FIELD_KEYS.has(field.key)) groups.models.push(field)
    else if (GATES_FIELD_KEYS.has(field.key)) groups.gates.push(field)
    else groups.dispatch.push(field)
  }
  return groups
}

/** The one sentence that replaces the rank / cap mode / pin controls (spec
 *  2026-09-20 §5.3), read from the capacity READOUT so it shows the effective
 *  cap the governor actually set, not the stored pin. One sentence with middle
 *  dots, the link rendered beside it; never an em dash (house rule). */
function capacityLine(row: CapacityProduct | undefined): string {
  if (!row) return 'Capacity: rank, cap mode and pin are set on Delivery → Capacity.'
  const rank = row.rank === null ? 'unranked' : `rank ${row.rank}`
  const cap = row.capacityMode === 'pinned' ? `pinned (pin ${row.pin})` : `auto (effective cap ${row.effectiveCap})`
  return `Capacity: ${rank} · ${cap} · ${row.inFlight} in flight.`
}

function ImplementationEligibility({ label, decision }: { label: string; decision: ProductImplementationDecision }) {
  const reasons = decision.constraints.filter((constraint) => constraint.state !== 'permitted')
  return (
    <div className="flex flex-col gap-1">
      <p className="font-medium">{label}: {decision.state}</p>
      {decision.approvalRequired && <p>Approval required before implementation.</p>}
      {reasons.length > 0 && (
        <ul className="list-disc pl-4">
          {reasons.map((reason) => (
            <li key={reason.code}>
              {reason.link ? <a href={reason.link} className="underline">{reason.text}</a> : reason.text}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function AutomationPolicyCard({ product, project }: { product: ProductNode; project: Project }) {
  const { data: schema, isLoading, isError } = usePolicySchema()
  const update = useUpdateProductPolicy(product.slug)
  const coverage = useProductPolicyCoverage(product.slug)
  const implementation = useProductImplementationPolicy(product.slug)
  const live = project.onboardingState === 'live'
  const policy = project.automationPolicy ?? {}
  const gates = useSdlcGates()
  // Unpolled: the roadmap card is not a live surface; `QK.capacity` is
  // invalidated by every dial mutation (spec D10), which is what keeps it honest.
  const capacity = useCapacity()
  const capacityRow = capacity.data?.products.find((p) => p.slug === product.slug)

  // A failed schema fetch is a real failure, not a slow one: without the banner
  // the card renders its loading skeleton forever, so an operator is left waiting
  // on a request that already gave up (the LeafList idiom).
  if (isError && !schema) {
    return (
      <section aria-labelledby="automation-heading" className="flex flex-col gap-2">
        <h3 id="automation-heading" className="text-xs font-medium uppercase text-ink-muted">Automation</h3>
        <Banner tone="danger" title="Could not load the automation settings.">
          They refresh on their own, so they will fill in once the connection is back.
        </Banner>
      </section>
    )
  }
  if (isLoading || !schema) {
    return (
      <section aria-labelledby="automation-heading">
        <h3 id="automation-heading" className="mb-2 text-xs font-medium uppercase text-ink-muted">Automation</h3>
        <Skeleton lines={2} />
      </section>
    )
  }

  const fields = orderedFields(schema)
  // Defaults are for display only; never persist absent keys as explicit choices.
  const effective: Record<string, unknown> = {}
  for (const f of fields) effective[f.key] = policy[f.key] ?? f.default

  // The tolerance the loop ACTS under: the configured value only while the
  // process kill switch is on (server `effective_tolerance`); otherwise cautious.
  const configured = String(effective.risk_tolerance ?? 'cautious')
  const enabled = gates.data?.autoresolveEnabled ?? true
  const acting = enabled ? configured : 'cautious'
  const verdict = (g: GateSpec): string =>
    g.risk === null ? 'Policy-owned' : g.autoResolvesUnder[acting] ? 'Automatic' : 'Waits for you'

  function handleChange(key: string, val: unknown) {
    if (update.isPending) return
    update.mutate({ policy: { [key]: val }, expectedUpdatedAt: project.automationPolicyUpdatedAt })
  }

  function renderField(field: ConfigField) {
    return field.type === 'bool'
      ? (
        <div key={field.key}>
          <ConfigInput field={field} value={effective[field.key]} onChange={handleChange} />
          {field.help && <p className="text-xs text-ink-muted mt-0.5">{field.help}</p>}
        </div>
      )
      : (
        <Field key={field.key} label={field.label} htmlFor={field.key} help={field.help || undefined}>
          <ConfigInput field={field} value={effective[field.key]} onChange={handleChange} />
        </Field>
      )
  }

  const groups = groupFields(fields)

  return (
    <section aria-labelledby="automation-heading" className="flex flex-col gap-2">
      <h3 id="automation-heading" className="text-xs font-medium uppercase text-ink-muted">Automation</h3>
      {/* This has to outrank the quiet muted-text treatment below: a fine-print
          line at the top of a long card reads as boilerplate, so people scroll
          past it, hit a wall of disabled fields lower down, and report it as
          broken (#2094). A banner is the loud version of the same `!live` gate. */}
      {!live && (
        <Banner
          tone="info"
          title="Automation settings can be edited once the product is live."
          className="sticky top-0 z-raised"
        />
      )}
      {/* A rejected save must say so: without it the control springs back to its
          old value on the next refetch with no explanation of why. */}
      {update.isError && (
        <Banner
          tone="danger"
          title={extractMutationErrorMessage(update.error, 'Could not save the automation settings')}
        />
      )}
      {live && project.automationPolicy === null && (
        <p className="text-sm text-ink-muted">
          Issue dispatch is off until you explicitly change Dispatch from off.
        </p>
      )}
      {/* A native fieldset disables every descendant control at once — ConfigInput
          itself carries no `disabled` prop (it is shared verbatim with the
          automations Config tab, which never disables a field). */}
      <fieldset disabled={!live || update.isPending} className="flex flex-col gap-4 disabled:opacity-60">
        <fieldset className="flex flex-col gap-3">
          <legend className="text-sm font-medium">Dispatch</legend>
          {groups.dispatch.map(renderField)}
        </fieldset>
        <fieldset className="flex flex-col gap-3">
          <legend className="text-sm font-medium">Models</legend>
          {groups.models.map(renderField)}
        </fieldset>
        <fieldset className="flex flex-col gap-3">
          <legend className="text-sm font-medium">Gates</legend>
          {groups.gates.map(renderField)}
        </fieldset>
      </fieldset>
      {/* Informational, not a control: it stays enabled and visible even while
          the fieldset above it is disabled (spec 2026-09-20 D5). */}
      <p className="text-xs text-ink-muted">
        {capacityLine(capacityRow)}{' '}
        <Link to={deliveryPath('capacity')} className="text-accent hover:underline">Adjust on Delivery → Capacity</Link>
      </p>
      {/* Every gate the tolerance governs, with what the selected level does to it
          (spec 2026-09-14 §11, built by 2026-09-17 D5). The verdict column is the
          answer to "if I pick balanced, what stops asking me?". */}
      <section aria-labelledby="gates-heading" className="mt-2 flex flex-col gap-2 border-t border-edge pt-3">
        <h4 id="gates-heading" className="text-sm font-medium">Gates under {configured}</h4>
        {!enabled && configured !== 'cautious' && (
          <Banner tone="info" title="Auto-resolve is switched off for this deployment, so every gate waits for you." />
        )}
        {gates.isPending ? (
          <p className="text-sm text-ink-muted" role="status">Checking gates…</p>
        ) : gates.isError || !gates.data ? (
          <p className="text-sm text-ink-muted" role="status">Gate list is unavailable.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase text-ink-muted">
                <th scope="col" className="pb-1 font-medium">Gate</th>
                <th scope="col" className="pb-1 font-medium">Risk</th>
                <th scope="col" className="pb-1 font-medium">Under {acting}</th>
              </tr>
            </thead>
            <tbody>
              {gates.data.gates.map((g) => (
                <tr key={g.id} className="border-t border-edge align-top">
                  <td className="py-1 pr-2">
                    <p>{g.label}</p>
                    <p className="text-xs text-ink-muted">{g.action}</p>
                  </td>
                  {/* A `risk: null` row is not "no risk": it is owned by a policy
                      knob rather than the tolerance, which the verdict column
                      spells out as "Policy-owned". */}
                  <td className="py-1 pr-2 text-ink-muted">{g.risk ?? 'policy'}</td>
                  <td className="py-1">{verdict(g)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section aria-labelledby="implementation-policy-heading" className="mt-2 flex flex-col gap-2 border-t border-edge pt-3">
        <h4 id="implementation-policy-heading" className="text-sm font-medium">Issue implementation eligibility</h4>
        {implementation.isPending ? (
          <p className="text-sm text-ink-muted" role="status">Checking implementation policy…</p>
        ) : implementation.isError || !implementation.data ? (
          <p className="text-sm text-ink-muted" role="status">Implementation policy is unavailable.</p>
        ) : (
          <>
            <p className="text-sm text-ink-muted">{implementation.data.explanation}</p>
            <p className="text-xs text-ink-muted">
              Observed <time dateTime={implementation.data.observedAt}>{implementation.data.observedAt}</time>
            </p>
            <p className="text-sm text-ink-muted">
              Manual implementation is a one-time request. It does not turn on automatic dispatch or bypass required checks.
            </p>
            {implementation.data.repositories.length === 0 ? (
              <p className="text-sm text-ink-muted">No repository implementation decisions are available.</p>
            ) : (
              <ul className="flex flex-col gap-3 text-sm">
                {implementation.data.repositories.map((repo) => (
                  <li key={repo.requestedRepo} className="min-w-0 break-words flex flex-col gap-2">
                    <p className="font-medium">{repo.requestedRepo}</p>
                    <ImplementationEligibility label="Automatic implementation" decision={repo.decisions.automaticImplementation} />
                    <ImplementationEligibility label="Automatic proposals" decision={repo.decisions.automaticProposal} />
                    <ImplementationEligibility label="Manual implementation" decision={repo.decisions.manualImplementation} />
                    {[...new Set(Object.values(repo.decisions)
                      .filter((decision) => decision.remainingChecks.length > 0)
                      .map((decision) => decision.note))].map((note) => (
                      <p key={note} className="text-xs text-ink-muted">{note}</p>
                    ))}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>
      <section aria-labelledby="repository-coverage-heading" className="mt-2 flex flex-col gap-2 border-t border-edge pt-3">
        <h4 id="repository-coverage-heading" className="text-sm font-medium">Repository coverage</h4>
        {coverage.isPending ? (
          <p className="text-sm text-ink-muted" role="status">Checking repository coverage…</p>
        ) : coverage.isError || !coverage.data ? (
          <p className="text-sm text-ink-muted" role="status">Repository coverage is unavailable.</p>
        ) : (
          <>
            <p className="text-sm text-ink-muted">{coverage.data.explanation}</p>
            <p className="text-xs text-ink-muted">
              Observed <time dateTime={coverage.data.observedAt}>{coverage.data.observedAt}</time>
            </p>
            {coverage.data.repositories.length === 0 ? (
              <p className="text-sm text-ink-muted">No repositories are registered for this product.</p>
            ) : (
              <ul className="flex flex-col gap-3 text-sm">
                {coverage.data.repositories.map((repo) => (
                  <li key={repo.requestedRepo} className="min-w-0 break-words">
                    <p className="font-medium">{repo.requestedRepo} · {repo.state}</p>
                    {repo.canonicalRepo && repo.canonicalRepo !== repo.requestedRepo && (
                      <p className="text-ink-muted">Canonical repository: {repo.canonicalRepo}</p>
                    )}
                    <p>{repo.reason}</p>
                    {repo.candidates.length > 0 && (
                      <ul aria-label={`Registered products for ${repo.requestedRepo}`} className="flex flex-wrap gap-x-3 gap-y-1">
                        {repo.candidates.map((candidate) => (
                          <li key={candidate.id}>
                            <Link to={productPath(candidate.slug)} className="underline">{candidate.slug}</Link>
                          </li>
                        ))}
                      </ul>
                    )}
                    {repo.unresolvedRepos.length > 0 && (
                      <p className="text-ink-muted">Unverified registrations: {repo.unresolvedRepos.join(', ')}</p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>
    </section>
  )
}
