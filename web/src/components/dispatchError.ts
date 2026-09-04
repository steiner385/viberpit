/**
 * What `DispatchBar`'s `onDispatch` rejects with when there is something to
 * SAY about the failure. The bar shows the message inline above the prompt
 * and keeps the draft. Any other rejection also keeps the draft but says
 * nothing — that is the shape for failures another surface already owns
 * (the shell's "all accounts limited" banner).
 *
 * Its own module (not `DispatchBar.tsx`) so the component file exports only
 * components, which is what keeps fast refresh working.
 */
export class DispatchError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'DispatchError'
  }
}
