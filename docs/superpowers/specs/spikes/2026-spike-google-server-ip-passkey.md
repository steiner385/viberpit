# Spike B — Google accepts a server-resident passkey

**Date:** 2026-06-28
**Question:** Will Google honor a passkey created and asserted by a *server-resident* virtual
authenticator — for both registration and a full passwordless sign-in — headlessly, from the
server, with no human gesture?
**Verdict:** ✅ PASS (both halves), on an established spare account from this host.

## Method

- Standalone debug Chromium (CDP `:9222`) on the host's native `:0` display; user typed the
  bootstrap login for a spare real Google account (`kindashdemo@…`, NOT a Claude account).
- Attached a CDP virtual authenticator (`ctap2`/`internal`, `isUserVerified` +
  `automaticPresenceSimulation`), drove Google's *Create a passkey* dialog → Google stored the
  public key (account now lists "passkey 1"); we captured the resident credential + private key
  (`rpId: google.com`, `hasPrivKey: true`).
- Logged out (explicit `/Logout`), re-injected the credential via `WebAuthn.addCredential` into
  a fresh authenticator, selected the account, clicked the passkey-challenge "Continue".

## Result

- Registration: **accepted, no gesture.**
- Authentication: reached `signin/challenge/pk/presend` (Google offered the **passkey** path,
  no password field), the assertion fired (signCount 1 → 3), and the browser **landed at
  `myaccount.google.com` signed in, with no password and no secondary challenge.**

## Caveats (do not over-read)

1. **One account, one host, one run.** Google's risk engine is stochastic; a cold IP, an
   unestablished account, odd hours, or repeated automated logins could still escalate. PASS
   means it *can* work cleanly, not that it *always* will.
2. **Established-device effect.** The sign-in happened in the same on-host profile that
   registered the passkey (cookies cleared, but same device fingerprint). A brand-new profile
   on a new host may be treated more suspiciously — worth a follow-up before trusting it on the
   real Claude accounts.
3. **The spare account ≠ the real accounts.** Workspace policies / account age on `primary` /
   `kindash` may differ. Validate on a real target (carefully) before Phase 3 relies on it.
4. **UI drive is brittle.** Phase 3 must reliably click Google's account chip + passkey
   "Continue" (obfuscated DOM); selectors will need maintenance.

## Bottom line

The host-resident-passkey approach is **viable as the Phase 3 primary path** — Google honored a
server-held passkey end-to-end, headlessly. The `op`/password fallback can be the rare
exception. Recommended next: repeat on a fresh on-host profile (cold-device check) and, with
care, on a real target account, before building Phase 3 in earnest.
