# Robust Archaeology Coverage — Design

**Date:** 2026-09-02
**Status:** Approved design, pre-implementation
**Builds on:** `2026-08-31-pm-product-layer-design.md` (§5 archaeology onboarding, §6 classify/drift), which shipped in #897 (Phase 2).

## 1. Problem & goal

Dogfooding the Phase-2 archaeology onboarding on ccswitch itself (register → mirror 897 work items → propose-map → merge #901's 21-feature map) produced a strong map of everything *actively developed* but **omitted stable, foundational capabilities**: the CLI core, the eponymous per-account `CLAUDE_CONFIG_DIR` isolation / `use` switch primitive, and credential-store encryption. Those were added by hand in #907.

Root cause: `server/pm/classification.py::build_map_synthesis_prompt` uses **PR-churn concentration + module topology as the *discovery* signal** ("churn concentration in the PR history is strong evidence of real capability areas"). That tracks *activity*, not *existence* — the most settled primitives churn least, so a churn-weighted pass structurally under-represents them. **Churn ≠ importance.**

**Goal:** make archaeology's coverage complete and bias-resistant, so onboarding a new product (KinDash next) reconstructs the durable *and* the stable-foundational capabilities, with an auditable coverage claim the human curator can check.

## 2. Design principles

1. **Census before churn.** Enumerate the complete set of capability *surfaces* structurally first; use churn only to prioritize/describe/assign lifecycle, never to *discover*. Coverage comes from the census; churn adds depth and state.
2. **Read the project's own authoritative docs.** README, `CLAUDE.md`/`AGENTS.md`, constitution/specs/ADRs name the fundamentals outright ("a CLI core plus a web interface", "account = `CLAUDE_CONFIG_DIR`"). Ground the map in stated intent, not just inference.
3. **Multi-lens sweep.** Independent discovery lenses so no single bias drops a whole class of capability.
4. **Mandatory coverage audit.** Every census surface maps to ≥1 feature doc, or is explicitly declared not-a-feature. No silent omission.
5. **Name the anti-bias rule.** Instruct the agent that low-churn code is *often the most fundamental* — near-zero churn on a census surface is a reason to scrutinize for a missing foundation, not to skip it.

## 3. Changes

### 3.1 Rework `build_map_synthesis_prompt` (the 80/20)
Restructure the `scope="initial"` seed prompt into ordered, required steps (the `scope="diff"` drift branch is unchanged except it inherits the same coverage discipline for the capability it touches):

- **A — Read authoritative docs first.** README(s), `CLAUDE.md`/`AGENTS.md`, `docs/`, `.specify/`/constitution/ADRs. Extract stated purpose + named capabilities/principles as ground truth.
- **B — Structural capability census (churn-independent, language-aware).** Enumerate every surface:
  - **CLI / entry points:** `[project.scripts]`/`console_scripts` (pyproject/setup.cfg), argparse/click/typer subcommands, `bin/` scripts, Make targets, package.json `bin`.
  - **Web / API:** routers, route groups, page/tab registries, GraphQL schema.
  - **Background:** automation/scheduler/worker registries, cron, queues, services.
  - **Data & config:** DB models/tables/migrations, config schema, env-var/settings surface.
  - **Library API:** top-level package exports.
  Each distinct surface cluster is a candidate capability, independent of churn.
- **C — Churn + backlog enrichment (secondary).** Use PR-churn concentration, commit scopes, and the open/closed backlog to prioritize, assign lifecycle state, and find in-flight work + gaps — not to discover.
- **D — Synthesize** the outcome-template docs (unchanged: Problem / Target users / Desired outcome / Success metrics / Non-goals / Notes / Evidence; frontmatter `links` = feature-to-feature only).
- **E — Coverage audit (anti-omission gate).** Emit a table mapping every census item (each CLI subcommand, API router, automation, model cluster, console entry) → the feature doc(s) covering it; anything uncovered gets a doc or an explicit "not a feature" line. The audit goes in the **PR body** so the human reviewer sees the coverage claim.
- **Explicit rule text:** *"PR-churn signals activity, not importance. Stable, rarely-churned code (CLI, auth, storage, isolation primitives, config) is frequently the most fundamental — include it. A census surface with near-zero recent churn is a reason to scrutinize for a MISSING foundational feature, not to skip it."*

### 3.2 Completeness-critic pass (independent second agent)
After the map-synthesis session opens its PR, spawn a second, independent "coverage critic" session: given the repo + the drafted feature docs, walk the entry points / routers / models / CLI and list any capability with **no** doc. Output posts as PR review comments on the map PR (or feeds a follow-up diff via the drift path). This is the *completeness-critic* quality pattern — a cheap adversarial check targeting exactly this failure mode. Reuses the existing session-spawn seam; modes stay propose-only.

### 3.3 Curated corrections as ground truth
Extend the existing "past human corrections surfaced to classifier prompts" (design §6) to the **map-synthesis** prompt: per-project (the drift path re-reads the merged map) and cross-project exemplars ("look for a CLI, a credential store, an isolation primitive…"), so hand-added foundations like #907's teach future passes.

### 3.4 Archaeology eval
Add a case to the SDLC eval harness (`scripts/run_evals.py` + `evals/`): run archaeology on a fixture repo (or ccswitch) and assert the coverage audit maps all `console_scripts` + API routers + automations. Prevents method regression.

## 4. Phasing

Each phase independently useful; each its own plan + PR series.

1. **Prompt rework (§3.1)** — census-first + anti-bias rule + coverage audit in the PR body. One-file change to `build_map_synthesis_prompt`; improves every future onboarding immediately. *Would have caught the CLI/config-dir/crypto gaps.*
2. **Completeness-critic pass (§3.2)** — the second-agent adversarial coverage check, wired into onboarding/drift.
3. **Correction feedback (§3.3) + archaeology eval (§3.4).**

## 5. Validation

Re-run archaeology on ccswitch via the **drift/propose-map path** after Phase 1 and confirm the coverage audit surfaces the CLI, config-dir isolation, and credential store — i.e., the method now catches what #907 hand-added.

## 6. Out of scope

- Rewriting already-merged feature docs (curation handles those; #907 covered ccswitch's gaps).
- Changing the feature-doc schema or the classify/triage path (unchanged).
- Non-GitHub projects; auto mode (human gate stays constitutional).
