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

## Web app smoke

A quick end-to-end check of the ccswitch web UI. Full scenario coverage is in
`specs/001-ccswitch-web/quickstart.md`.

### Prerequisites
- At least two accounts registered (`ccswitch add alpha`, `ccswitch add beta`).
- Backend running: `CCSWITCH_MASTER_KEY=... uvicorn server.app:app --host 127.0.0.1 --port 8787`
- Web assets built: `cd web && npm run build` (or dev server: `npm run dev`).
- Optionally: set up the nginx vhost via `deploy/ccswitch-tailnet.nginx` (see `deploy/README.md`) to reach from a phone over HTTPS.

### Steps
1. Open `http://127.0.0.1:8787` (or the Tailscale HTTPS URL).
   - Verify: the Accounts strip shows `alpha` and `beta` with session/weekly usage percentages.
   - Verify: no "All accounts at limit" banner (if one appears, confirm reset time is in local time).
2. Click **New** → create a session (name optional, account "Auto-select").
   - Verify: the session appears in the Sessions list with status **Working** or **Idle**.
3. Click the session row to open the terminal.
   - Verify: the xterm.js terminal renders the `claude` banner/prompt.
   - Verify: the helper-key bar (Ctrl-C, Escape, etc.) is visible on mobile/narrow viewports.
   - Type a short prompt; confirm output appears.
4. Press **Back** (or navigate back) to return to the dashboard.
   - Verify: keyboard focus returns to the session row.
5. Open the overflow menu (⋯) for the session → **Rename** → save.
   - Verify: the new name is reflected immediately in the list.
6. Open the overflow menu → **Close** → confirm the session is removed from the list.
7. With all accounts at limit (or all `limited` status): click **New**.
   - Verify: the "All accounts at limit" banner appears with the earliest reset time.

### Automated equivalents
`cd web && npm run test` covers the management surfaces (T057 a11y tests included).
`pytest server/tests/` covers the API/WS paths. See `specs/001-ccswitch-web/quickstart.md` for
the full scenario matrix.

---

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
