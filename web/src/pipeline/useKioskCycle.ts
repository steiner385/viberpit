// Kiosk auto-cycle (spec §5): advance the focused repo every cycleSeconds,
// paused while the tab is hidden — mirrors plotroom App.tsx:84-91 kiosk cycling
// (which ticks a nonce and re-pins Delivery; here we rotate the focused pipeline
// so the ported PipelineView's focused-first ordering walks each repo in turn).
import { useEffect } from 'react';

export function useKioskCycle(
  kiosk: boolean, cycleSeconds: number, repos: readonly string[],
  focused: string | null, focus: (repo: string) => void,
) {
  useEffect(() => {
    if (!kiosk || repos.length < 2) return;
    const id = setInterval(() => {
      if (document.hidden) return; // paused while the tab is not visible
      const i = focused ? repos.indexOf(focused) : -1;
      focus(repos[(i + 1) % repos.length]!);
    }, cycleSeconds * 1000);
    return () => clearInterval(id);
  }, [kiosk, cycleSeconds, repos, focused, focus]);
}
