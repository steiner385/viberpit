"""AI-native SDLC orchestration — pure cores for the intent→spec→plan chain.

Phase 1 of the AI-Native SDLC design
(`docs/superpowers/specs/2026-08-29-ai-native-sdlc-design.md`). This package holds
the **pure, I/O-free** decision cores that mirror the existing automation mold
(`pr_doctor_engine` / `backlog_prompt`):

- ``engine``  — the stage-decision state machine (``next_action``).
- ``prompts`` — per-stage seed-prompt builders.

Neither module imports the orchestrator or the DB; the Intake/Synthesizer loops
inject their I/O around these cores.
"""
