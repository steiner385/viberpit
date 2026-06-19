# Manual smoke test (uses one real Max account)

1. `ccswitch add alpha`  → log in to your first account when the browser opens.
   - Verify: `ls -la ~/.claude-accounts/store/alpha.cred.json` (mode 600) exists.
   - Verify: `ccswitch list` shows `alpha  ok  session=N% ... weekly=M% ...` with reset times in ET.
2. `ccrun -- -p "say hi in 3 words"`  → prints a normal Claude response.
   - Verify sync-back: note `expiresAt` in `store/alpha.cred.json` before/after; if Claude refreshed, it bumped.
3. Repeat `ccswitch add beta` / `add gamma` for the other accounts.
4. `ccswitch list` shows all three with live utilization.
5. Calibrate detection (Task 7 note): once any account nears its limit, run `ccrun -- -p "..."`
   against it and capture the limited result JSON; confirm `detect_limit_in_result` matches
   (add a fixture to `tests/test_detect.py` if the wording differs).
6. Interactive: `ccmax` launches on the most-headroom account; confirm the session works.

## Accounts that sign in with Google ("Continue with Google")

ccswitch works unchanged with Google-federated Claude accounts. Google is only the
identity provider for claude.ai at login time; after `claude auth login` completes,
Claude Code holds a normal **Anthropic** OAuth token (`claudeAiOauth` with a
`refreshToken`). ccswitch stores and refreshes *that* token via
`platform.claude.com/v1/oauth/token` — it never stores or refreshes a Google token.

The only practical care is the browser's Google session when capturing several
Google-federated accounts:

- Run `ccswitch add <name> --email you@gmail.com`. The `--email` pre-fills the
  Anthropic page, which routes you to Google for that address.
- In the "Continue with Google" step, pick the **correct** Google account (use
  "Use another account" if it isn't listed).
- If your browser stays signed into one Google account, sign out of Google (or use a
  fresh/incognito profile) between adds so you don't capture the wrong account.

This login is a manual browser flow you perform yourself — nothing automated touches
Google, so it is unrelated to (and unaffected by) any rules about automating Google
OAuth.
