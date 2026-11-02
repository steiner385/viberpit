/**
 * The session's account select plus its Unpin button — the one control for
 * moving a live session to another account. Shown in the session rail's
 * Environment card.
 */
import { Pin } from 'lucide-react'
import { TOUCH_FLOOR } from '@/ui/list/listControlStyles'

export interface AccountSwitcherProps {
  account: string
  accounts: { name: string; nickname?: string | null }[]
  accountPinned: boolean
  onSelectAccount: (name: string) => void
  /** Clear the account pin; the server rotates a `limited` session immediately. */
  onUnpin?: () => void
}

/** Focus ring, plus the coarse-pointer touch floor (docs/ACCESSIBILITY.md §4):
 *  44px where `hover: hover` does not match, unchanged under a mouse. */
const CONTROL = `focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent ${TOUCH_FLOOR}`
/** The icon-only Unpin button needs the width floor too (the `ListSearchInput` idiom). */
const ICON_WIDTH_FLOOR = 'min-w-11 [@media(hover:hover)]:min-w-0'

export function AccountSwitcher({ account, accounts, accountPinned, onSelectAccount, onUnpin }: AccountSwitcherProps) {
  return (
    <>
      <select
        aria-label="Switch account"
        value={account}
        onChange={(e) => e.target.value && e.target.value !== account && onSelectAccount(e.target.value)}
        className={`bg-transparent font-medium text-ink rounded ${CONTROL}`}
      >
        {accounts.every((a) => a.name !== account) && <option value={account}>{account}</option>}
        {accounts.map((a) => (
          <option key={a.name} value={a.name}>{a.nickname || a.name}</option>
        ))}
      </select>
      {accountPinned && (
        <button
          type="button"
          onClick={onUnpin}
          aria-label="Unpin account"
          title="Pinned — auto-rotation off. Click to unpin."
          className={`inline-flex items-center justify-center text-warning rounded ${CONTROL} ${ICON_WIDTH_FLOOR}`}
        >
          <Pin size={14} aria-hidden="true" />
        </button>
      )}
    </>
  )
}
