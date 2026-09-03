import { RefreshCw } from 'lucide-react'
import { Banner } from '@/ui/Banner'
import { Button } from '@/ui/Button'

interface NewVersionBannerProps {
  onReload: () => void
}

export function NewVersionBanner({ onReload }: NewVersionBannerProps) {
  return (
    <Banner
      tone="info"
      title="New version available"
      icon={<RefreshCw size={16} aria-hidden="true" />}
      actions={<Button size="sm" onClick={onReload}>Reload</Button>}
      className="fixed inset-x-0 top-0 z-50 items-center justify-center rounded-none border-x-0 border-t-0 px-4 py-2"
    />
  )
}
