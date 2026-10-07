# Investigation is not resolution

**Symptom:** A coding session diagnoses a reported malfunction, then finishes without addressing the clear remedy or asking for a concrete blocker.

**Anti-pattern:** Completion guidance covers explicit implementation requests but leaves ordinary troubleshooting as diagnosis-only; engine-specific patches would drift.

**Fix shape:** Put task follow-through in the shared session instruction composer, independent of the completion tool/marker transport. Continue clear, authorized, directly related fixes through verification and the repository PR workflow. Explicit investigation-only, review, planning, read-only, permission and automation handoff constraints still win. An unresolved blocker needs a concrete question, not a completion signal.

**Where to look:** `server/sessions/tools/instruction.py`, Claude's `_build_append_string`, and Codex's `_fresh_thread_params`. The sibling audit found intentional read-only workflows in `server/automations/backlog_prompt.py` and `server/sdlc/prompts.py`; these keep their limits. The assistant drawer uses its own role instructions and is outside this coding-session policy.

The approach follows the autonomy/persistence section of the [OpenAI Codex prompting guide](https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide). This is guidance, not a deterministic guarantee of model behavior. Structural tests cover both engines and every tool/marker combination; behavioral cases live in `docs/superpowers/evals/session-tools/cases.jsonl`.

Rollout: new coding sessions receive the policy after the updated daemon is running. Existing Codex threads retain their original developer instructions on resume; this change does not rewrite them or restart active sessions.

Verification on 2026-10-07: 38 focused instruction/engine integration tests passed. The real Codex app-server accepted `thread/start` with the composed guidance for both tool and marker variants in disposable, unauthenticated, ephemeral threads. A separate live behavioral fixture was attempted with `codex exec`, but the API returned 401 (missing authentication) before any model response; neither the fix nor investigate-only behavioral arm was validated. Acceptance of instructions must not be reported as proof of behavioral improvement.
