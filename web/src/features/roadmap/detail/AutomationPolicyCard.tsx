// The product Overview's Automation card (product automation policy phase 1
// design §9): every `PRODUCT_POLICY_SCHEMA` field rendered through the SAME
// `ConfigInput` the automations Config tab uses, reading `project.automationPolicy`
// (the raw stored blob, or null when the product never opted in) against the
// server's defaults. Disabled — with an explanatory note — until the product is
// live: the onboarding wizard above it is what gets it there.
import type { ConfigField } from '@/api/automations'
import type { Project } from '@/api/pm'
import type { ProductNode } from '@/api/roadmap'
import { usePolicySchema, useUpdateProductPolicy } from '@/api/hooks'
import { ConfigInput } from '@/features/delivery/automation/ConfigInput'
import { Field } from '@/ui/Field'
import { Skeleton } from '@/ui/Skeleton'

/** `dispatch` first — the on/off knob for a product's automation is the first
 *  thing a human should see, not buried among caps and weights. */
function orderedFields(schema: ConfigField[]): ConfigField[] {
  const dispatch = schema.filter((f) => f.key === 'dispatch')
  const rest = schema.filter((f) => f.key !== 'dispatch')
  return [...dispatch, ...rest]
}

export function AutomationPolicyCard({ product, project }: { product: ProductNode; project: Project }) {
  const { data: schema, isLoading } = usePolicySchema()
  const update = useUpdateProductPolicy(product.slug)
  const live = project.onboardingState === 'live'
  const policy = project.automationPolicy ?? {}

  if (isLoading || !schema) {
    return (
      <section aria-labelledby="automation-heading">
        <h3 id="automation-heading" className="mb-2 text-xs font-medium uppercase text-ink-muted">Automation</h3>
        <Skeleton lines={2} />
      </section>
    )
  }

  const fields = orderedFields(schema)
  // Every schema key with its current-or-default value: the server never sees a
  // partial blob (`update_project_policy` stores exactly what it validates).
  const effective: Record<string, unknown> = {}
  for (const f of fields) effective[f.key] = policy[f.key] ?? f.default

  function handleChange(key: string, val: unknown) {
    update.mutate({ ...effective, [key]: val })
  }

  return (
    <section aria-labelledby="automation-heading" className="flex flex-col gap-2">
      <h3 id="automation-heading" className="text-xs font-medium uppercase text-ink-muted">Automation</h3>
      {!live && (
        <p className="text-sm text-ink-muted">Automation starts once the product is live.</p>
      )}
      {live && project.automationPolicy === null && (
        <p className="text-sm text-ink-muted">
          Not opted in — the first change turns automation on for this product with these values.
        </p>
      )}
      {/* A native fieldset disables every descendant control at once — ConfigInput
          itself carries no `disabled` prop (it is shared verbatim with the
          automations Config tab, which never disables a field). */}
      <fieldset disabled={!live} className="flex flex-col gap-3 disabled:opacity-60">
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
    </section>
  )
}
