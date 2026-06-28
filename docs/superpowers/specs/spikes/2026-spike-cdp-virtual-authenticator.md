# Spike A — CDP virtual authenticator

**Date:** 2026-06-28
**Question:** Can a CDP-attached virtual WebAuthn authenticator complete a full register +
assert ceremony **headlessly, with no human gesture**, in the kind of Chromium the reauth flow
drives?
**Verdict:** ✅ PASS (plumbing proven). Probe: `spike-a-cdp-virtual-authenticator.py` (this dir).

## Result

Launched headless Chrome-for-Testing 148 (`/usr/local/bin/chromium`) with remote debugging,
attached a virtual authenticator via the CDP `WebAuthn` domain, and ran
`navigator.credentials.create()` then `.get()` against a `http://localhost` RP:

```
virtual authenticator id: 9dd73e1d-8bb5-4903-8f65-c70eace52810
ceremony result: {'ok': True, 'credId': '_HoGTa_aMAsQE_nGmFuqJF8WleTTs0OFcIfnUUEWkFE', 'asserted': True}
PASS: virtual authenticator completed create()+get() headlessly, no gesture
```

Authenticator options that stood in for the human: `protocol:"ctap2"`, `transport:"internal"`,
`hasResidentKey:true`, `hasUserVerification:true`, `isUserVerified:true`,
`automaticPresenceSimulation:true`. With these, both create and get resolved with no prompt.

## Implementation notes (gotchas confirmed)

- **`--remote-allow-origins=*` is mandatory** on Chrome 111+ or the CDP websocket handshake is
  rejected (HTTP 403 on upgrade). Without it the probe can't connect at all.
- **WebAuthn needs a secure context.** `about:blank` / `data:` URLs have a null origin and are
  rejected; the page must be served from `http://localhost` (the non-HTTPS secure-context
  exception) so `rp.id:"localhost"` is valid.
- No extra Python deps needed — raw CDP over `requests` (+ `/json` target discovery) and
  `websockets`, both already in the venv.

## What this did NOT prove (explicit gaps → later spikes/tasks)

1. **Persistence across sessions.** This minted a fresh credential per run. Phase 3 needs to
   store the private key and re-inject it via `WebAuthn.addCredential` on each launch — UNTESTED
   here. (Phase 3 task: PasskeyVault + addCredential round-trip.)
2. **Attaching to the `claude auth login` launcher's browser specifically.** The probe launched
   its own Chrome. Phase 3 must attach to (or supply the profile to) the browser the
   `BROWSER`/`ccswitch-reauth-browser` launcher spawns — UNTESTED.
3. **A real relying party.** `localhost` accepted the assertion unconditionally. Whether
   **Google** accepts a virtual-authenticator assertion (esp. with `automaticPresenceSimulation`)
   or escalates is the dominant external risk → **Spike B**, still pending a throwaway account.
4. **Keyring-backed key storage / reboot survival** → **Spike C**, pending a reboot window.

## Bottom line

The mechanism is viable: we can drive a software passkey headlessly via CDP. The remaining
risk is entirely about whether a *real RP (Google)* honors it — which Spike A cannot answer and
Spike B must.
