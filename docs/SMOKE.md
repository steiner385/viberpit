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
