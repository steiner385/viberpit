---
name: capture-learning
description: Capture a confirmed, reusable development discovery after a correction, incident, or repeated workflow reveals knowledge that should survive the current agent session.
---

# Capture Durable Learning

Use this only for verified knowledge that will improve later work. Do not promote
speculation, transient task state, credentials, personal paths, or one-off details.

1. Search existing instructions, skills, ADRs, project docs, fix-pattern references,
   tests, and hooks for the same knowledge. Update the existing source instead of
   creating a parallel copy.
2. Classify the discovery:
   - Always needed in most sessions: concise shared instruction.
   - Repeatable procedure with a recognizable trigger: focused skill.
   - Architectural rationale or tradeoff: ADR or maintained project document.
   - Recurring defect shape: fix-pattern reference plus a regression test when useful.
   - Deterministic requirement: hook, linter, type check, or test.
   - Temporary or personal fact: provider-local memory only.
3. Keep startup instructions small. Link to detailed material or load it through a
   skill instead of adding long incident histories to the always-on context.
4. Run `python3 .agents/agentctl.py render .` and
   `python3 .agents/agentctl.py check .` when the Portable Agent Contract is present.
5. Report what was promoted, where it now lives, and what evidence made it durable.
