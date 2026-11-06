import { Input } from '@/ui/Input'
import { Button } from '@/ui/Button'

/**
 * A path input plus its Reset-to-default affordance, as the ONE control a
 * SettingRow holds. It forwards `aria-describedby` to the input the way
 * `ui/Checkbox` does: the row clones its child to attach the description id, and
 * a bare wrapping `<span>` would swallow it — leaving the input undescribed and
 * the description announced on nothing.
 */
export function PathControl({ id, value, placeholder, resetLabel, onChange, invalid = false, ...aria }: {
  id: string
  value: string
  placeholder: string
  resetLabel: string
  onChange: (next: string) => void
  /** The row shows an error for this value: the input is marked `aria-invalid`. */
  invalid?: boolean
  'aria-describedby'?: string
}) {
  return (
    <span className="flex w-full items-center gap-2">
      <Input
        id={id}
        value={value}
        placeholder={placeholder}
        invalid={invalid}
        aria-describedby={aria['aria-describedby']}
        onChange={(e) => onChange(e.target.value)}
      />
      <Button
        type="button"
        variant="ghost"
        size="sm"
        aria-label={resetLabel}
        onClick={() => onChange('')}
        disabled={value === ''}
      >
        Reset
      </Button>
    </span>
  )
}
