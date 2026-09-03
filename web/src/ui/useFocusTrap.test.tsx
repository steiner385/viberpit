// Every test for this hook lives here. The original suite was ported from
// plotroom@5166b52 frontend/src/__tests__/useFocusTrap.test.tsx and sat in
// src/pipeline/__tests__/ until the hook moved to src/ui/; it was merged back
// here so one module's coverage is not split across two directories.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { useRef, useState, type RefObject } from 'react'
import { useFocusTrap } from './useFocusTrap'

function Host({ onClose }: { onClose: () => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useFocusTrap(ref, open, { onClose: () => { setOpen(false); onClose() } })
  return (
    <div id="root">
      <button onClick={() => setOpen(true)}>open</button>
      {open && (
        <div ref={ref} tabIndex={-1}>
          <button>first</button>
          <button>last</button>
        </div>
      )}
    </div>
  )
}

function NestedHost({ onOuterClose, onInnerClose }: { onOuterClose?: () => void; onInnerClose?: () => void } = {}) {
  const [outerOpen, setOuterOpen] = useState(false)
  const [innerOpen, setInnerOpen] = useState(false)
  const outerRef = useRef<HTMLDivElement>(null)
  const innerRef = useRef<HTMLDivElement>(null)
  useFocusTrap(outerRef, outerOpen, { onClose: () => { setOuterOpen(false); onOuterClose?.() } })
  useFocusTrap(innerRef, innerOpen, { onClose: () => { setInnerOpen(false); onInnerClose?.() } })
  return (
    <div id="root">
      <button onClick={() => setOuterOpen(true)}>open-outer</button>
      {outerOpen && (
        <div ref={outerRef} tabIndex={-1}>
          <button onClick={() => setInnerOpen(true)}>open-inner</button>
          <button onClick={() => setOuterOpen(false)}>close-outer</button>
          {innerOpen && (
            <div ref={innerRef} tabIndex={-1}>
              <button onClick={() => setInnerOpen(false)}>close-inner</button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

interface TrapHostProps {
  active: boolean;
  onClose: () => void;
  returnFocusRef?: RefObject<HTMLElement | null>;
}

/** Renders a dialog-like div with two focusable children and wires up useFocusTrap. */
function TrapHost({ active, onClose, returnFocusRef }: TrapHostProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  useFocusTrap(containerRef, active, { onClose, returnFocusRef });
  return (
    <div ref={containerRef} data-testid="container" tabIndex={-1}>
      <button data-testid="btn-first">First</button>
      <button data-testid="btn-last">Last</button>
    </div>
  );
}

/** Host with a SINGLE focusable child (edge-case: wrapping first = last). */
function TrapHostSingle({ active, onClose }: TrapHostProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  useFocusTrap(containerRef, active, { onClose });
  return (
    <div ref={containerRef} data-testid="container" tabIndex={-1}>
      <button data-testid="btn-only">Only</button>
    </div>
  );
}

/** Host with no focusable children — container itself should receive focus. */
function TrapHostEmpty({ active, onClose }: TrapHostProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  useFocusTrap(containerRef, active, { onClose });
  return (
    <div ref={containerRef} data-testid="container" tabIndex={-1} />
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('useFocusTrap', () => {
  it('moves focus in, sets inert on #root, wraps Tab, restores focus and clears inert on close', () => {
    let closed = 0
    render(<Host onClose={() => { closed++ }} />)
    const opener = screen.getByText('open')
    opener.focus()
    fireEvent.click(opener)
    const root = document.getElementById('root')!
    expect(document.activeElement).toBe(screen.getByText('first'))
    expect(root.hasAttribute('inert')).toBe(true)
    screen.getByText('last').focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(screen.getByText('first'))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(closed).toBe(1)
    expect(root.hasAttribute('inert')).toBe(false)
    expect(document.activeElement).toBe(opener)
  })

  it('keeps #root inert while a nested trap is stacked, and only clears it once the outer trap closes', () => {
    render(<NestedHost />)
    const root = document.getElementById('root')!

    fireEvent.click(screen.getByText('open-outer'))
    expect(root.hasAttribute('inert')).toBe(true)

    fireEvent.click(screen.getByText('open-inner'))
    expect(root.hasAttribute('inert')).toBe(true)

    // Closing the inner (nested) trap must NOT lift inert — the outer trap is
    // still active. A boolean (rather than depth-counted) implementation would
    // fail this assertion by removing the attribute here.
    fireEvent.click(screen.getByText('close-inner'))
    expect(root.hasAttribute('inert')).toBe(true)

    fireEvent.click(screen.getByText('close-outer'))
    expect(root.hasAttribute('inert')).toBe(false)
  })

  it('one Escape closes only the innermost stacked trap', () => {
    // Every active trap installs its own `document` keydown listener.
    // `stopPropagation()` stops other *nodes*, not sibling listeners on the same
    // node, so one Escape used to invoke both `onClose` callbacks — and because
    // listeners fire in registration order (outermost first), the outer trap is
    // the one that gets to swallow the event. Only the trap on top of the stack
    // may act on Escape.
    const outer = vi.fn()
    const inner = vi.fn()
    render(<NestedHost onOuterClose={outer} onInnerClose={inner} />)
    fireEvent.click(screen.getByText('open-outer'))
    fireEvent.click(screen.getByText('open-inner'))

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(inner).toHaveBeenCalledTimes(1)
    expect(outer).not.toHaveBeenCalled()
    expect(screen.queryByText('close-inner')).toBeNull()
    expect(screen.getByText('close-outer')).toBeInTheDocument()

    // With the inner trap gone the outer is back on top and takes the next one.
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(outer).toHaveBeenCalledTimes(1)
    expect(inner).toHaveBeenCalledTimes(1)
  })

  // ── Ported suite (was src/pipeline/__tests__/useFocusTrap.test.tsx) ──────

  // 1. Focus moves into container on activate
  it('moves focus to the first focusable element when activated', async () => {
    const { getByTestId } = render(<TrapHost active={true} onClose={() => {}} />);
    const first = getByTestId('btn-first');
    await waitFor(() => {
      expect(document.activeElement).toBe(first);
    });
  });

  it('falls back to focusing the container itself when there are no focusable children', async () => {
    const { getByTestId } = render(<TrapHostEmpty active={true} onClose={() => {}} />);
    const container = getByTestId('container');
    await waitFor(() => {
      expect(document.activeElement).toBe(container);
    });
  });

  it('does NOT move focus when inactive', () => {
    const { getByTestId } = render(<TrapHost active={false} onClose={() => {}} />);
    const container = getByTestId('container');
    expect(document.activeElement).not.toBe(container);
    expect(document.activeElement).not.toBe(getByTestId('btn-first'));
  });

  // 2. Esc calls onClose
  it('calls onClose when Escape is pressed while active', () => {
    const onClose = vi.fn();
    render(<TrapHost active={true} onClose={onClose} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does NOT call onClose for Escape when inactive', () => {
    const onClose = vi.fn();
    render(<TrapHost active={false} onClose={onClose} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
  });

  // 3. Return focus on deactivate
  it('returns focus to returnFocusRef.current when deactivated', () => {
    const trigger = document.createElement('button');
    document.body.appendChild(trigger);
    trigger.focus();
    const returnFocusRef = { current: trigger };

    const { rerender } = render(
      <TrapHost active={true} onClose={() => {}} returnFocusRef={returnFocusRef} />,
    );
    // Deactivate
    rerender(<TrapHost active={false} onClose={() => {}} returnFocusRef={returnFocusRef} />);
    expect(document.activeElement).toBe(trigger);
    trigger.remove();
  });

  it('returns focus to returnFocusRef on unmount', () => {
    const trigger = document.createElement('button');
    document.body.appendChild(trigger);
    trigger.focus();
    const returnFocusRef = { current: trigger };

    const { unmount } = render(
      <TrapHost active={true} onClose={() => {}} returnFocusRef={returnFocusRef} />,
    );
    unmount();
    expect(document.activeElement).toBe(trigger);
    trigger.remove();
  });

  it('returns focus to previousFocusElement when returnFocusRef is absent', () => {
    const trigger = document.createElement('button');
    document.body.appendChild(trigger);
    trigger.focus();

    const { rerender } = render(<TrapHost active={true} onClose={() => {}} />);
    // At this point focus has moved into the container. Deactivate:
    rerender(<TrapHost active={false} onClose={() => {}} />);
    expect(document.activeElement).toBe(trigger);
    trigger.remove();
  });

  // 4. Tab on last focusable → wraps to first
  it('Tab on the last focusable element wraps focus to the first', () => {
    const { getByTestId } = render(<TrapHost active={true} onClose={() => {}} />);
    const first = getByTestId('btn-first');
    const last = getByTestId('btn-last');

    last.focus();
    expect(document.activeElement).toBe(last);

    const tabEvent = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    document.dispatchEvent(tabEvent);

    expect(document.activeElement).toBe(first);
  });

  it('Tab in the middle (not on last) does NOT wrap focus', () => {
    const { getByTestId } = render(<TrapHost active={true} onClose={() => {}} />);
    const first = getByTestId('btn-first');

    first.focus();
    expect(document.activeElement).toBe(first);

    const tabEvent = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    document.dispatchEvent(tabEvent);

    // Focus should remain on first (not moved by the trap; would naturally move to last on real Tab)
    expect(document.activeElement).toBe(first);
  });

  // 5. Shift-Tab on first focusable → wraps to last
  it('Shift-Tab on the first focusable element wraps focus to the last', () => {
    const { getByTestId } = render(<TrapHost active={true} onClose={() => {}} />);
    const first = getByTestId('btn-first');
    const last = getByTestId('btn-last');

    first.focus();
    expect(document.activeElement).toBe(first);

    const shiftTabEvent = new KeyboardEvent('keydown', {
      key: 'Tab', shiftKey: true, bubbles: true, cancelable: true,
    });
    document.dispatchEvent(shiftTabEvent);

    expect(document.activeElement).toBe(last);
  });

  it('Shift-Tab on the last element (not first) does NOT wrap focus', () => {
    const { getByTestId } = render(<TrapHost active={true} onClose={() => {}} />);
    const last = getByTestId('btn-last');

    last.focus();

    const shiftTabEvent = new KeyboardEvent('keydown', {
      key: 'Tab', shiftKey: true, bubbles: true, cancelable: true,
    });
    document.dispatchEvent(shiftTabEvent);

    // Focus stays on last (trap only wraps when on first, for shift-tab)
    expect(document.activeElement).toBe(last);
  });

  // Edge case: single focusable child — wrapping stays on that same element
  it('Tab on the only focusable element wraps back to itself', () => {
    const { getByTestId } = render(<TrapHostSingle active={true} onClose={() => {}} />);
    const only = getByTestId('btn-only');

    only.focus();
    const tabEvent = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true });
    document.dispatchEvent(tabEvent);

    expect(document.activeElement).toBe(only);
  });
})
