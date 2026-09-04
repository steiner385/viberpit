/**
 * The one accessible-name convention for a badged navigation control.
 *
 * Extracted verbatim from the private `tabName()` that lived in `Tabs.tsx`, so
 * the tab strip and the shell's nav regions (`NavItem`, and the `NavBar`/`TopBar`
 * built on it) speak the same sentence to a screen reader: "Sessions (3)",
 * "Settings (needs attention)", "Archived (3, needs attention)". Duplicating the
 * string in the shell would have let the two drift the moment either changed.
 *
 * It returns the *whole* name rather than a label plus a decoration to append,
 * because a name assembled from two sibling elements is not the same name: the
 * accname algorithm trims each element's contribution, so a label span next to a
 * " (3)" span is announced "Sessions(3)". Every caller renders the full string
 * into a single node.
 */
export interface BadgedNameParts {
  label: string
  /** Falsy (including 0) means no count is announced. */
  count?: number
  /** Announces "needs attention"; pairs with the amber dot. */
  attention?: boolean
}

export function badgedName({ label, count, attention }: BadgedNameParts): string {
  if (attention) return count ? `${label} (${count}, needs attention)` : `${label} (needs attention)`
  return count ? `${label} (${count})` : label
}
