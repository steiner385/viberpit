import { cn } from '@/lib/utils'

/** The compact control chrome every list-controls strip shares — the group/sort
 *  selects and the search box (`ListSearchInput`). One definition, because the
 *  search box now lives in its own module and a second copy of this string is
 *  how the two would drift apart. */
export const listControlClass = cn(
  'text-xs rounded-md border border-gray-200 dark:border-gray-700',
  'bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300',
  'px-2 py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
  'disabled:opacity-60',
)
