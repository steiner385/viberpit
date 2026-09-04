import { RefreshCw } from 'lucide-react'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'

interface NewVersionBannerProps {
  onReload: () => void
}

/** Renders in flow inside the shell's `BannerStack` — never `fixed`, so it
 *  pushes the page down rather than covering its header or the toaster band. */
export function NewVersionBanner({ onReload }: NewVersionBannerProps) {
  return (
    <Banner
      tone="info"
      title="New version available"
      icon={<RefreshCw size={16} aria-hidden="true" />}
      actions={<Button size="sm" onClick={onReload}>Reload</Button>}
      className="items-center"
    />
  )
}
