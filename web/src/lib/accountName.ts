/**
 * accountName — client-side mirror of the backend account-name rule so the
 * Add-account dialog can validate as you type instead of round-tripping to a
 * 400 that surfaces as a generic toast.
 *
 * SOURCE OF TRUTH is the Python regex in `src/ccswitch/account_names.py`
 * (`_RE = ^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$`). Keep this in lockstep with it.
 * The server still re-validates — this is UX guidance, not a security boundary.
 */

/** Mirrors `_RE` in account_names.py. */
const ACCOUNT_NAME_RE = /^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$/

/**
 * Return an error string if `name` is not a valid account identifier, or null
 * if it is valid. Empty input returns null (treated as "not yet entered" — the
 * Add button is gated separately on non-empty).
 */
export function accountNameError(name: string): string | null {
  if (name === '') return null
  if (name === '.' || name === '..') return 'Account name cannot be "." or "..".'
  if (name.length > 64) return 'Account name is too long (max 64 characters).'
  if (!ACCOUNT_NAME_RE.test(name)) {
    if (name.includes('@')) {
      return "That looks like an email — use a short identifier instead (no @). You can add a nickname afterwards."
    }
    return 'Use only letters, numbers, and . _ - (must start with a letter, number, or _).'
  }
  return null
}
