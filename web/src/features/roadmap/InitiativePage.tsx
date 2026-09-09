import { useParams } from 'react-router-dom'
import { useInitiative } from '@/api/hooks'
import { PageHeader } from '@/ui/PageHeader'
import { PageContent } from '@/ui/PageContent'
import { EmptyState } from '@/ui/EmptyState'
import { Breadcrumb, type BreadcrumbItem } from '@/ui/Breadcrumb'
import { ROADMAP } from '@/features/products/vocab'
import { productPath, roadmapPath } from './roadmapNavState'

/** `/initiatives/:initiativeSlug` — the initiative detail, a full-width page
 *  that reaches Roadmap through its breadcrumb (roadmap spec §4). Task 12 fills
 *  the body. */
export function InitiativePage() {
  const { initiativeSlug = '' } = useParams()
  const { data: initiative, isLoading } = useInitiative(initiativeSlug || null)
  const title = initiative?.title ?? initiativeSlug
  const crumbs: BreadcrumbItem[] = [
    { label: ROADMAP.label, to: roadmapPath('initiatives') },
    ...(initiative?.projectSlug ? [{ label: initiative.projectSlug, to: productPath(initiative.projectSlug) }] : []),
    { label: title },
  ]
  return (
    <div className="flex h-full min-h-0 flex-col" data-page-variant="standard">
      <PageHeader title={title} meta={<Breadcrumb items={crumbs} />} />
      <PageContent busy={isLoading}>
        <EmptyState title="Loading…" />
      </PageContent>
    </div>
  )
}
