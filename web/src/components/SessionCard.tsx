import * as React from 'react'
import { useState, useRef } from 'react'
import { Pencil, Trash2, EllipsisVertical } from 'lucide-react'
import { cn } from '@/lib/utils'
import { StatusPill, UsageBadge } from './UsageBadge'
import type { Session, Account } from '@/api/types'

interface SessionCardProps {
  session: Session
  account: Account | undefined
  onOpen: (id: string) => void
  onRename: (id: string) => void
  onClose: (id: string) => void
}

export function SessionCard({ session, account, onOpen, onRename, onClose }: SessionCardProps) {
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const menuButtonRef = useRef<HTMLButtonElement>(null)

  // Close menu on outside click
  React.useEffect(() => {
    if (!menuOpen) return
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [menuOpen])

  function handleRename() {
    setMenuOpen(false)
    onRename(session.id)
  }

  function handleClose() {
    setMenuOpen(false)
    onClose(session.id)
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      onOpen(session.id)
    }
  }

  return (
    <article
      className={cn(
        'flex items-center gap-3 rounded-lg border border-gray-200 dark:border-gray-700',
        'bg-white dark:bg-gray-800 p-3 min-h-[44px]',
        'focus-within:ring-2 focus-within:ring-blue-500',
      )}
    >
      {/* Main tappable area — opens the session */}
      <button
        type="button"
        id={`row-${session.id}`}
        className={cn(
          'flex-1 min-w-0 text-left min-h-[44px] flex flex-col justify-center gap-1',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded',
        )}
        onClick={() => onOpen(session.id)}
        onKeyDown={handleKeyDown}
        aria-label={`Open session ${session.name}`}
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-semibold text-gray-900 dark:text-gray-100 truncate">
            {session.name}
          </span>
          <span className="text-xs text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5 rounded">
            {session.accountName}
          </span>
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          <StatusPill status={session.status} />
          {account && (
            <>
              <UsageBadge pct={account.usage.session.pct} label="session" />
              <UsageBadge pct={account.usage.weekly.pct} label="weekly" />
            </>
          )}
        </div>
      </button>

      {/* Overflow action menu */}
      <div className="relative flex-shrink-0" ref={menuRef}>
        <button
          ref={menuButtonRef}
          type="button"
          className={cn(
            'h-11 w-11 flex items-center justify-center rounded-md',
            'text-gray-500 dark:text-gray-400',
            'hover:bg-gray-100 dark:hover:bg-gray-700',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500',
          )}
          aria-label={`Actions for session ${session.name}`}
          aria-haspopup="true"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((v) => !v)}
        >
          <EllipsisVertical size={16} aria-hidden="true" />
        </button>

        {menuOpen && (
          <div
            className={cn(
              'absolute right-0 top-full mt-1 z-10 min-w-[140px]',
              'rounded-md border border-gray-200 dark:border-gray-600',
              'bg-white dark:bg-gray-800 shadow-lg py-1',
            )}
            role="menu"
            aria-label={`Actions for ${session.name}`}
          >
            <button
              type="button"
              role="menuitem"
              className={cn(
                'flex w-full items-center gap-2 px-3 py-2 text-sm min-h-[44px]',
                'text-gray-700 dark:text-gray-200',
                'hover:bg-gray-100 dark:hover:bg-gray-700',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500',
              )}
              onClick={handleRename}
            >
              <Pencil size={14} aria-hidden="true" />
              Rename
            </button>
            <button
              type="button"
              role="menuitem"
              className={cn(
                'flex w-full items-center gap-2 px-3 py-2 text-sm min-h-[44px]',
                'text-red-600 dark:text-red-400',
                'hover:bg-red-50 dark:hover:bg-red-900/20',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500',
              )}
              onClick={handleClose}
            >
              <Trash2 size={14} aria-hidden="true" />
              Close
            </button>
          </div>
        )}
      </div>
    </article>
  )
}
