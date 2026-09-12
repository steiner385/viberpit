import { cn } from '@/lib/utils'

/** The compact control chrome every list-controls strip shares — the group/sort
 *  selects and the search box (`ListSearchInput`). One definition, because the
 *  search box now lives in its own module and a second copy of this string is
 *  how the two would drift apart. */
export const listControlClass = cn(
  'text-xs rounded-md border border-edge',
  'bg-surface text-ink',
  'px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
  'disabled:opacity-60',
)
