import { SettingsSection } from './chassis/SettingsSection'
import { LiveReadoutCard } from './chassis/LiveReadoutCard'
import type { SettingsDef } from './settingsDefs'

/**
 * DEC-12: an unmigrated section rendered inside the chassis heading. Its
 * internals are untouched; PRs 5b–5e replace each `component` with a real
 * section and this wrapper disappears with the last one.
 *
 * The point is that the PAGE is consistent from day one — every section has the
 * same h2, one-sentence description and scope badge — without twelve sections
 * having to be rewritten in one change. A def's `readout` mounts the live
 * capacity card after the body (Hosts, Accounts), so those sections gain it
 * without their own components learning a hook.
 */
export function HostedSection({ def }: { def: SettingsDef }) {
  const Body = def.component
  return (
    <SettingsSection id={def.id} title={def.label} description={def.description} scope={def.scope}>
      <Body />
      {def.readout && <LiveReadoutCard highlight={def.readout} />}
    </SettingsSection>
  )
}
