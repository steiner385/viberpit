# /goal conformance spike verdicts (2026-06-22)

Built-in `/goal <condition>` (singular) is verified present in the Claude Code binary
(strings in versions/2.1.186: "No goal set. Usage: /goal <condition>", "/goal clear to
stop early"). It has NO structured channel in claude-agent-sdk==0.2.105 (no SystemMessage
subtype / hook / message class carries goal state — confirmed by inspecting the installed
package). Capture is therefore done at our own dispatcher.

## Gate G.1 — `/goal <condition>` sent via passthrough engages the built-in feature: PENDING

Assumed TRUE (working assumption for Tasks 2–5). To confirm, an operator with a live Max
account runs the existing slash spike harness against a trusted cwd and sends "/goal echo
done", checking the stream/CLI acknowledges a goal was set (vs. the text being treated as a
plain model turn).

Decision gate: if FALSE, the badge would be cosmetic — STOP and revisit before shipping the
frontend (Task 5). Backend capture (Tasks 2–4) is still valid as a record of UI-requested
goals regardless.

## Gate G.2 — workspace trust permits /goal: ASSUMED TRUE

Sessions are pre-trusted via configdir.trust_project() in the orchestrator before the SDK
client launches, and we set no hook-restriction flags, so /goal should not be trust-blocked.

## Gate G.3 — catalog classification: CONFIRMED

`goal` is not in EXCLUDED and not in CLIENT/CONTROL, so classify("goal") == "passthrough"
(server/sessions/commands/catalog.py). Correct: we want it forwarded to the CLI after capture.
