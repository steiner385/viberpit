# Reauth cookie-copy feasibility gate — VERDICT: PASS (with 2 corrections)

Ran on the host (DISPLAY=:0) with the REAL google-chrome 149 launched (via Playwright
launchPersistentContext, executablePath=/usr/bin/google-chrome) on a copy of
~/.config/google-chrome/Default with the Claude cookie rows stripped.

## Result
- **Claude: logged OUT** ✓ — claude.ai/login shows "Continue with Google / email" picker.
- **Google: logged IN** ✓ — myaccount.google.com shows the account dashboard (no password prompt) —
  **ONLY when chrome is launched with `--password-store=gnome-libsecret`**. Without that flag,
  Chrome used its basic password store, could not decrypt the keyring-encrypted copied cookies,
  and Google appeared logged out (false fail).

## Two corrections to the spec/plan (folded in)
1. **Strip pattern**: Claude's cookies are on `claude.com` / `claudeusercontent.com` /
   `platform.claude.com` — NOT `claude.ai`. The strip MUST be
   `DELETE FROM cookies WHERE host_key LIKE '%claude%' OR host_key LIKE '%anthropic%'`.
2. **Launcher** MUST pass `--password-store=gnome-libsecret` so Chrome decrypts the copied
   Google cookies via the keyring ("Chrome Safe Storage" entry confirmed present).

Approach validated: cookie-copy keeps Google logged in + forces Claude re-pick. No fallback needed.
