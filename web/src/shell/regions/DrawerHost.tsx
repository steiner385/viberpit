import type { ReactNode } from 'react'
import { Sheet } from '@/ui/Sheet'
import { SidebarResizer } from '@/components/SidebarResizer'
import { useDrawerName, useCloseDrawer, type DrawerName } from '@/shell/drawerParam'
import { DRAWER_MIN, DRAWER_MAX, useDrawerWidth } from '@/shell/drawerWidth'
import { useViewport } from '@/shell/useViewport'
import { AssistantSheet } from '@/features/assistant/AssistantSheet'
import { AssistantGestureProvider, useAssistantGestureHandle } from '@/features/assistant/assistantGestures'
import { HelpSheet } from './HelpSheet'

/**
 * `app.drawer` — ONE `Sheet`, its content chosen by `?drawer=` (sub-spec B §4.2).
 *
 * ## Why one host and not one component per drawer
 *
 * Each drawer mounting its own `Sheet` would allow two open at once, and would
 * give each its own answer to "overlay or push", "how wide", and "how does
 * closing spell the URL". Here those are answered once, and `DRAWERS` is a
 * `Record<DrawerName, …>`, so a new name added to `drawerParam.ts` fails to
 * compile until it has content and a title.
 *
 * ## An unknown value is not a drawer
 *
 * `useDrawerName()` returns `null` for anything not in `DRAWER_NAMES`, and this
 * component renders NOTHING for `null` — no wrapper, no empty sheet, no region
 * marker. A stale `?drawer=assistantv2` link must land on the page, not on a
 * modal with nothing in it that the user has to dismiss to reach the app.
 *
 * ## Placement (D13)
 *
 * From 1280px the drawer is a layout column (`mode="push"` — inline, no
 * backdrop, no focus trap, resizable) and below it an overlay sheet. The
 * threshold itself is `useViewport().drawerPushes`, because `useViewport` is the
 * only module allowed to read a layout media query (there is a source-walking
 * guard in `useViewport.test.tsx`) and 1280 falls between its width tiers.
 * Whether the drawer is mounted AT ALL in a given mode — kiosk hides it — is the
 * shell's region-visibility matrix (Task 16), not this component's business, for
 * the same reason `PromptDock` does not check the mode either.
 *
 * ## `data-region` rides on the panel
 *
 * As an overlay the panel portals to `#overlays`, outside the layout tree. The
 * marker therefore goes on the panel rather than on a wrapper left behind in the
 * grid, so `[data-region="app.drawer"]` always names the thing on screen. Push
 * mode adds a wrapper (see below) but keeps the marker where it is, so the
 * region is the same node in both modes.
 */

const DRAWERS: Record<DrawerName, { title: string; content: ReactNode }> = {
  help: { title: 'Help & Shortcuts', content: <HelpSheet /> },
  assistant: { title: 'Assistant', content: <AssistantSheet /> },
}

export function DrawerHost() {
  // Wraps the whole component so both this component's own `onClose` (below) and the
  // nested `AssistantSheet` (whenever the assistant drawer is the one open) share the
  // same gesture ref — see assistantGestures.tsx.
  return (
    <AssistantGestureProvider>
      <DrawerHostInner />
    </AssistantGestureProvider>
  )
}

function DrawerHostInner() {
  const drawer = useDrawerName()
  const close = useCloseDrawer()
  const { drawerPushes } = useViewport()
  const [width, setWidth] = useDrawerWidth()
  const assistantGestures = useAssistantGestureHandle()

  if (!drawer) return null

  const { title, content } = DRAWERS[drawer]

  // Esc normally just closes the drawer (Sheet's focus trap calls `onClose` on Escape
  // in overlay mode — see useFocusTrap.ts). When the assistant drawer is open with a
  // live turn, Esc should stop that turn first and leave the drawer open, matching
  // the in-session Esc/interrupt behavior instead of abandoning the turn silently.
  const onClose = () => {
    if (drawer === 'assistant' && assistantGestures.current?.turnLive) {
      assistantGestures.current.interrupt()
      return
    }
    close()
  }

  const sheet = (
    <Sheet
      open
      onClose={onClose}
      title={title}
      side="right"
      mode={drawerPushes ? 'push' : 'overlay'}
      width={width}
      data-region="app.drawer"
    >
      {content}
    </Sheet>
  )

  // As an overlay there is no resizer and the panel portals to `#overlays`, so
  // there is nothing to wrap and a wrapper would be an empty node left behind in
  // the layout grid.
  if (!drawerPushes) return sheet

  return (
    // The push-mode drag handle is INSIDE a landmark, not a sibling of every
    // landmark — the identical defect Task 17 fixed for `ContextList`, whose
    // handle sat outside them all (see the comment beside its `SidebarResizer`).
    // It is a focusable `role="separator"`, so as a bare sibling of `<nav>`,
    // `<aside>` and `<main>` it was page content in no landmark at all: axe
    // `region`, the rule this branch turned fully on. The panel itself is a
    // `role="dialog"`, which axe excludes from `region` — the handle is not, so
    // it needs this wrapper rather than a place inside the dialog's scroll body.
    //
    // `complementary` is the honest role: in push mode the drawer is an inline
    // supporting column beside `<main>`, not a modal. Named from the drawer's
    // own title so it is distinct from the list column's `complementary`
    // (axe `landmark-unique`).
    <aside aria-label={title} className="flex min-h-0 shrink-0">
      <SidebarResizer
        width={width}
        onResize={setWidth}
        panelSide="right"
        min={DRAWER_MIN}
        max={DRAWER_MAX}
        label="Resize drawer"
      />
      {sheet}
    </aside>
  )
}
