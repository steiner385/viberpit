/**
 * The DOM id of a row's open control — ONE producer, so every party that has to
 * name the same element agrees on the string: `CardFrame`, which renders it, and
 * whoever focuses it afterwards (`features/roadmap/useTreeKeys`) or points at it
 * (`DispatchBar`'s `aria-activedescendant`).
 *
 * ## Why `scope` exists
 *
 * Two containers can render the SAME row key at once. The Roadmap's list column
 * shows an initiative row in its tree while the detail pane beside it shows the
 * same initiative in `detail/InitiativeList` — two elements minting `row-<key>`,
 * which is invalid HTML (duplicate ids) and, worse, makes
 * `document.getElementById` a coin toss decided by DOM order. The tree's
 * ArrowDown then "worked" only because its row happened to come first; with the
 * panes the other way round, focus silently went nowhere.
 *
 * A scope namespaces the id to its container. Omitted means the unscoped
 * `row-<id>` — which is correct for a container that is the only one rendering
 * its ids (the sessions list), and is the form `aria-activedescendant` already
 * points at.
 */
export function rowElementId(id: string, scope?: string): string {
  return scope ? `row-${scope}-${id}` : `row-${id}`
}
