// The product Overview's Automation card (product automation policy phase 1
// design §9): every `PRODUCT_POLICY_SCHEMA` field rendered through the SAME
// `ConfigInput` the automations Config tab uses, reading `project.automationPolicy`
// (the raw stored blob, or null when the product never opted in) against the
// server's defaults. Disabled — with an explanatory note — until the product is
// live: the onboarding wizard above it is what gets it there.
import { Link } from 'react-router-dom'
import { productPath } from '../roadmapNavState'
import type { ConfigField } from '@/api/automations'
import type { Project, ProductImplementationDecision } from '@/api/pm'
import type { ProductNode } from '@/api/roadmap'
import { usePolicySchema, useUpdateProductPolicy, useProductPolicyCoverage, useProductImplementationPolicy } from '@/api/hooks'
import { extractMutationErrorMessage } from '@/api/queryClient'
import { ConfigInput } from '@/features/delivery/automation/ConfigInput'
import { Banner } from '@/ui/Banner'
import { Field } from '@/ui/Field'
import { Skeleton } from '@/ui/Skeleton'

/** `dispatch` first — the issue-dispatch setting is the first
 *  thing a human should see, not buried among caps and weights. */
function orderedFields(schema: ConfigField[]): ConfigField[] {
  const dispatch = schema.filter((f) => f.key === 'dispatch')
  const rest = schema.filter((f) => f.key !== 'dispatch')
  return [...dispatch, ...rest]
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

  function handleChange(key: string, val: unknown) {
    if (update.isPending) return
    update.mutate({ policy: { [key]: val }, expectedUpdatedAt: project.automationPolicyUpdatedAt })
  }

  return (
    <section aria-labelledby="automation-heading" className="flex flex-col gap-2">
      <h3 id="automation-heading" className="text-xs font-medium uppercase text-ink-muted">Automation</h3>
      {!live && (
        <p className="text-sm text-ink-muted">Automation settings can be edited once the product is live.</p>
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
      <fieldset disabled={!live || update.isPending} className="flex flex-col gap-3 disabled:opacity-60">
        {fields.map((field) => (
          field.type === 'bool'
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
        ))}
      </fieldset>
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
