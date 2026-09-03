// Ported from plotroom@5166b52 frontend/src/hooks/useFocusTrap.ts; edits: render-phase ref write → useLayoutEffect (react-hooks/refs rule).
// Promoted to web/src/ui/ with an `inertRoot` option: while a trap is active it sets `inert` on
// an element (default `#root`) so background content is unfocusable/unclickable; a module-level
// depth counter allows nested traps to stack without the inner trap's close lifting `inert` early.
import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react';

/**
 * CSS selector for focusable elements.
 * Matches the exact selector used in the original ProtectionMap drawer trap.
 */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Module-level stack of active traps, outermost first. Every active trap
// installs its own `document` keydown listener, and `stopPropagation()` only
// stops other *nodes* — sibling listeners on the same node still run — so one
// Escape used to invoke every stacked trap's `onClose`. `stopImmediatePropagation()`
// alone does not fix it either: listeners fire in registration order, which is
// outermost-first, so the outer trap would swallow Escape and close the wrong
// dialog. Hence an explicit stack — only the trap on top acts on Escape, and it
// stops the event immediately so nothing after it (or on `window`) sees it.
const trapStack: symbol[] = [];

// Module-level depth counter: a dialog can open a second (nested) dialog, so
// the `inert` attribute must only be removed once the outermost trap closes.
let inertDepth = 0;
function setRootInert(id: string, on: boolean) {
  // Count first, unconditionally: increments and decrements must pair up even if
  // the element is missing at one end, or a decrement could be stolen from an
  // outer trap and lift `inert` while its dialog is still open.
  inertDepth = Math.max(0, inertDepth + (on ? 1 : -1));
  const root = document.getElementById(id);
  if (!root) return;
  if (inertDepth > 0) root.setAttribute('inert', '');
  else root.removeAttribute('inert');
}

/**
 * Shared focus-trap hook — extracted from ProtectionMap's inline drawer trap.
 *
 * When `active` is true:
 *  - Saves the currently focused element as the previous focus.
 *  - Moves focus to the first focusable element inside `containerRef.current`
 *    (or to the container itself, with tabIndex=-1 already expected on the container).
 *  - Listens for Escape → calls `opts.onClose`.
 *  - Intercepts Tab / Shift-Tab to wrap focus within the container.
 *
 * When `active` goes false (or the component unmounts):
 *  - Returns focus to `opts.returnFocusRef?.current` (if provided and non-null),
 *    otherwise to the element that had focus before the trap activated.
 */
export function useFocusTrap(
  containerRef: RefObject<HTMLElement | null>,
  active: boolean,
  opts: {
    onClose: () => void;
    returnFocusRef?: RefObject<HTMLElement | null>;
    /** Element id that becomes `inert` while the trap is active (default 'root'). Pass null to skip. */
    inertRoot?: string | null;
  },
): void {
  // Capture opts in a ref so the effect cleanup always sees the current values
  // without needing to include them in the dependency array (avoids re-running
  // the effect every time onClose/returnFocusRef identities change).
  const optsRef = useRef(opts);
  // useLayoutEffect (not render-phase assignment) to satisfy react-hooks/refs rule.
  useLayoutEffect(() => { optsRef.current = opts; });

  useEffect(() => {
    if (!active) return;

    // Save element that had focus before the trap activated.
    const previousFocus = document.activeElement as HTMLElement | null;

    const inertId = optsRef.current.inertRoot === undefined ? 'root' : optsRef.current.inertRoot;
    if (inertId) setRootInert(inertId, true);

    const token = Symbol('focus-trap');
    trapStack.push(token);

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        // Only the innermost trap closes: this listener also runs for outer
        // traps stacked beneath this dialog (see trapStack above).
        if (trapStack[trapStack.length - 1] !== token) return;
        // Swallow Escape here so it never reaches a background `window`-level
        // shortcut handler (e.g. SessionView's Esc-to-back), or an outer trap's
        // listener, while this trap is active — every hand-rolled dialog this
        // hook replaces did the same via its own per-dialog `stopPropagation()`.
        e.stopImmediatePropagation();
        optsRef.current.onClose();
        return;
      }

      if (e.key === 'Tab' && containerRef.current) {
        const focusable = [
          ...containerRef.current.querySelectorAll<HTMLElement>(FOCUSABLE),
        ];
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];

        if (e.shiftKey) {
          // Shift-Tab on the first element → wrap to last.
          if (document.activeElement === first) {
            e.preventDefault();
            last.focus();
          }
        } else {
          // Tab on the last element → wrap to first.
          if (document.activeElement === last) {
            e.preventDefault();
            first.focus();
          }
        }
      }
    };

    document.addEventListener('keydown', onKey);

    // Move focus into the container: prefer first focusable child, fall back to container.
    const focusTarget =
      containerRef.current?.querySelector<HTMLElement>(FOCUSABLE) ??
      containerRef.current;
    focusTarget?.focus();

    return () => {
      document.removeEventListener('keydown', onKey);
      // Splice by identity, not pop: teardown order is not guaranteed to be
      // LIFO (an outer dialog can unmount while an inner one is still open),
      // and popping would hand "innermost" to the wrong trap.
      const at = trapStack.indexOf(token);
      if (at !== -1) trapStack.splice(at, 1);
      if (inertId) setRootInert(inertId, false);
      // Return focus: explicit ref takes priority, then previous element.
      const returnTarget =
        optsRef.current.returnFocusRef?.current ?? previousFocus;
      returnTarget?.focus();
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);
}
