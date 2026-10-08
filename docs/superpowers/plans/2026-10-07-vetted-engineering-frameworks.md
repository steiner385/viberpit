# Vetted Engineering Frameworks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` to implement this plan task-by-task, with a fresh implementer and review between tasks. This document is a proposed execution plan; writing or merging it does not authorize framework admission or enable the proposed gates.

**Goal:** Make vetted engineering frameworks portable, measurable, and enforceable across ViberPit-managed projects, beginning with Impeccable and expanding only after candidate-specific evaluation.

**Architecture:** Extend the Portable Agent Contract for framework selection and pinned provenance, the existing SDLC gate registry for evidence prerequisites, and the existing eval corpus/runner for regression checks. Keep policy decisions pure; use existing API, automation, session, and Library boundaries for I/O. Third-party skills provide guidance; ViberPit owns applicability, evidence validation, and gate decisions.

**Tech Stack:** Existing Python/FastAPI/SQLAlchemy server, portable Python contract tooling, YAML eval corpus, and React/TypeScript views. Provider integration uses supported native engine surfaces. Reuse pytest, the existing static eval runner, and the project's browser test tooling.

**Spec:** `docs/superpowers/specs/2026-10-07-vetted-engineering-frameworks.md`

## Existing adoption and execution status (2026-10-08)

Impeccable already has practical pilots in **KinDash, FridgeSheet and ViberPit**.
See [historical pilot evidence](../../engineering/frameworks/impeccable-pilot-evidence.md)
for merged PRs, measured outcomes, limitations and prior hook activation. Reuse
this evidence and incumbent design context; do not repeat first-use onboarding.
PRs #2770, #2807 and #2808 delivered the initial lock, source verification and
offline dependency verification. They did not complete every admission item.

Task 4 has delivered browser acceptance calibration (#2809) and a restricted
native-agent source-review comparison (#2827, #2840). The completed six-call
subscription run detected both seeded defects with no false positives in either
arm. It exercised selected reference text, not full Impeccable invocation, visual
redesign, UI repair or a human assessment rubric; it does not establish a quality
advantage or satisfy the full promotion criteria. See
[comparison evidence](../../engineering/frameworks/impeccable-guidance-comparison.md).

Task 3 began observe-only. PR #2845 delivered pinned declaration reads and
untrusted-claim diagnostics. The next slice adds policy-based path applicability
and explicit GitHub review attestations bound to the PR revisions and declaration
digests. Lock declarations, ordinary approvals and agent reports cannot satisfy
review. Trusted reviewer identities require explicit policy configuration; the
initial ViberPit allowlist is empty. Trusted deterministic
runner receipts and merge-boundary enforcement remain subsequent work. Impeccable
stays a candidate.

The gate-modes slice adds `observe`, `evidence_required` and `enforced` modes
(entry default plus per-concern override) and declared exceptions bound to one
concern and one PR, with an owner other than the PR author, an expiry and
compensating evidence. A pure `framework_gate` decision holds SDLC artifact
merges and artifact-stage advances when a non-observe obligation lacks
evidence: required reviews need a current recorded review (a stale one blocks
but may be excepted; an invalid one may not), deterministic concerns stay
unsatisfied until trusted runner receipts are ingested, unknown scope blocks
only when enforced, revoked frameworks block only where the concern would
already block on scope (unless excepted), and advisory concerns never block. No
risk tolerance bypasses the block. The synthesizer reads the policy from the
default branch and the PR's scope and reviews before arming or advancing. A
failed policy read, or a failed PR read once a gating obligation is known,
blocks; the API observation applies the same rule. ViberPit's own lock stays
`observe`, so nothing changes for it today. Build-stage advances and code PRs
are not gated here; the merge-boundary required check is Task 6.

Two prerequisites apply before any repository leaves `observe`. First, policy
edits under `.agents/**` must be human-reviewed (CODEOWNERS plus branch
protection): an exception's `owner` is an unauthenticated string, so an
agent-authored lock PR could otherwise grant itself an exception or downgrade a
mode. Second, an armed auto-merge is not re-checked when new commits land; the
merge-boundary required check (Task 6) closes that, and the artifact-stage
advance re-check only catches it after merge.

Task 5 still needs actual native skill discovery/invocation, resume, rotation,
remote execution and revocation/rollback evidence. Task 6
assesses these existing deployments for standard enforcement; the backend-only
and no-contract onboarding negative cases still need proof. Checklist items below
remain incomplete unless their full exit criteria are demonstrated.

## Global Constraints

- Do not install an entire marketplace or update global account configuration as a shortcut. Approval applies to an immutable selected dependency closure.
- `.agents/contract.json` and `.agents/agentctl.py` remain canonical for portable instructions and projections. Preserve Impeccable's `provider_built` distinction.
- `server/sdlc/gates.py` remains the gate registry. Extend existing decisions and consumers; do not introduce a competing workflow engine or approval ladder.
- `scripts/run_evals.py --kind agent` lists cases; execution requires explicit `--execute` and a supported executable case. Never report list-only success as a behavioral evaluation pass, and do not generalize the restricted source-review comparison to full native skill compatibility.
- Check open PRs by issue and touched files before each implementation task. Search for existing schema, artifact, audit, and evidence storage before adding a model or helper.
- Keep `.claude/REVIEW.md` categories, severity, and separation of duties. Subjective critique is advisory unless a project explicitly adopts a testable requirement.
- Use failing tests before implementation. Run the repository's required lint/type/core/server gates for code changes, full web checks when touching the frontend, and live engine smoke tests for session integration. Follow the test-slot and environment-isolation instructions.
- Every proposed eval and test name below is a deliverable, not an assertion that it exists today. Resolve exact implementation filenames against the current checkout before each task.
- For a new DB model or column, include migration and reconciliation coverage on SQLite and Postgres. Preserve existing timestamp and cancellation helpers.

---

## PR 1 — Admission contract and inventory

### Task 1: Versioned framework selection and lock schema

**Files:**
- Modify: `.agents/contract.json`, `.agents/agentctl.py`
- Add: a framework lock manifest referenced by the contract, with its schema/validator colocated with the existing contract tooling
- Test: extend the existing agent-contract tests; add `evals/cases/framework-lock-integrity.yaml`

**Interfaces:** A resolved selection identifies upstream SHA, selected paths, complete references, provider projection hashes, executable dependency pins, compatibility, owner, and admission evidence. Effective policy has a stable digest; generated instructions and later UI consumers use the same resolved representation.

- [ ] Write failing `test_framework_lock_rejects_floating_or_missing_dependency` and `test_provider_built_projection_hashes` tests.
- [ ] Inventory current installed Impeccable and process skills. Record unknown provenance as unverified; do not fabricate an upstream SHA from a version string.
- [ ] Extend contract validation and rendering with backward-compatible handling for projects without framework metadata. Reject duplicate ownership, missing references, path traversal, escaping symlinks, and unapproved executable dependencies.
- [ ] Implement `framework-lock-integrity` as a static case consumed by `scripts/run_evals.py`.
- [ ] Verify `python .agents/agentctl.py check .` detects a deliberately changed fixture projection and passes after restoring the approved fixture.

**Exit proof:** Lock validation fails for tampered content and incomplete dependency closure; unchanged legacy contracts still validate. No runtime installation is enabled in this phase.

### Task 2: Impeccable admission report

**Files:** Add admission evidence under the project's existing documentation convention; update only the proposed selection/lock after review.

- [ ] Complete every spec vetting item for a concrete Impeccable revision, including scripts, hooks, binaries, downloads, telemetry, licenses, and both provider builds.
- [ ] Record approved execution permissions and the previous known-good package for rollback.
- [ ] Add `framework-impeccable-projection-integrity` to the static corpus. Verify both provider builds and all packaged references in clean disposable directories.
- [ ] Obtain an admission review distinct from the author before marking the revision pilot-ready.

**Exit proof:** The admission report links exact hashes and passing integrity results; missing executable provenance prevents promotion.

## PR 2 — Applicability and evidence at existing gates

### Task 3: Pure evidence decisions and trusted policy resolution

**Files:**
- Modify: `server/sdlc/gates.py`, `server/sdlc/engine.py`, `server/sdlc/prompts.py`, and their existing tests
- Modify as needed: `server/api/sdlc.py`, existing artifact/audit persistence and migration/reconciliation tests

**Interfaces:** A pure resolver takes project policy, task/change scope, effective lock digest, and evidence records; it returns applicable obligations, tier, status, and reason. Evidence identifies repository, revision, policy/framework digests, check/review scope, producer, artifacts, and finding dispositions.

**Observe-only delivery boundary:** An opt-in initiative detail request
(`GET /api/sdlc/initiatives/{slug}?includeFrameworkEvidence=true`) exposes framework
observations in the existing guidance payload. A read-only diagnostic request
(`POST /api/sdlc/initiatives/{slug}/framework-observations`) can compare submitted
supporting claims with a GitHub-resolved PR head and author. Neither endpoint
stores a review receipt, changes a transition, or accepts claims as attestations.
Both require the existing API identity control. A diagnostic request with a PR
number also collects its change scope and explicit review records from GitHub.

The repository default branch is resolved to a commit before reading its contract
and selected lock. A PR cannot weaken these observations by changing its own lock.
Git blob OIDs identify the declarations read; they are not a claim that the
installed package passed validation. Shared portable parsing supplies canonical
declaration digests and optional concern policies. Positive path matches establish
applicability; a negative requires a complete, stable, nonempty change scope whose
paths are all explicitly excluded. Unclassified paths and incomplete or renamed
scope remain unknown where old-path coverage is unavailable.

Only explicit structured records from configured GitHub reviewers can establish
`review_recorded`. They must match the repository, base/head, policy/framework
digests and complete scope; the provider-reviewed commit must match the head.
Reviewer identity comes from GitHub, is checked against the policy allowlist and
must differ from the PR author. A matching submitted diagnostic claim remains
untrusted. A recorded review is an authenticated principal's attestation, not proof
of model execution, a passing deterministic check or framework admission. Candidate
and revoked declarations stay visible. There is no new receipt database or gate
enforcement in this slice. Exceptions, trusted runner ingestion, native compatibility
and blocking enforcement still need subsequent implementation before the enforced
exit proof below is met.

- [x] Write `test_required_framework_evidence_blocks_transition`, `test_advisory_finding_does_not_block`, and `test_tolerance_cannot_skip_failed_evidence` before wiring new decisions (`server/tests/test_framework_gate.py`, `framework-gate-modes` eval).
- [x] Add negative cases for forged producer identity, author self-review, stale head, wrong repository, policy downgrade in the same PR, expired exception, and revoked framework (also self-issued and other-PR exceptions, and exceptions that cannot waive an invalid review).
- [ ] Wire prerequisites into existing stage decisions and explain output. Preserve current high-risk and release approvals. *Partial:* `next_action(..., framework_block=)` and the synthesizer gate artifact merges and artifact-stage advances. Cautious approvals and the build-stage advance are unchanged. Explain output reports `blocking`, `gateReasons` and per-obligation `mode`/`blocking` in `frameworkEvidence`. The detail endpoint's `nextAction` and the explain step simulation do not yet take the block.
- [ ] Authenticate evidence ingestion and validate it server-side. Separate agent-authored supporting artifacts from trusted runner attestations. *Still open:* GitHub review records are authenticated, but deterministic runner receipts are not ingested, so a `deterministic` concern in a non-observe mode always blocks.
- [x] Add `framework-gate-evidence-integrity` to `scripts/run_evals.py` using a deterministic fixture/check with no live service dependency (initial observe-only coverage in #2845; extend with each trust boundary).
- [x] Add the observe-only `framework-gate-receipts` case and `server/tests/test_framework_receipts.py`: independent reviewer/repository/revision bindings, conservative UI/process applicability, stale/self/wrong-repository rejection, and unchanged candidate state and stage decisions. This does not complete the blocking-gate tests above.

**Exit proof:** An eligible transition succeeds with fresh trusted evidence and fails with each invalid variant. Advisory findings never masquerade as deterministic failures.

## PR 3 — Executable evaluation harness and Impeccable pilot

### Task 4: Live evaluation execution with bounded cost

**Files:**
- Modify: `scripts/run_evals.py`, `server/sdlc/evals.py`, existing eval runner tests, `evals/README.md`
- Add: behavioral fixtures and result schema beside the existing eval corpus
- Modify as needed: existing session launch/driver integration for isolated eval execution

- [ ] Write `test_agent_eval_requires_executed_result` and `test_eval_timeout_or_empty_corpus_fails`. Add cancellation, unavailable-engine, missing-tool, and budget-exhaustion cases.
- [ ] Extend the runner with an explicit execution mode for live cases; preserve the existing list-only behavior with unmistakable output. Record engine/model, package/policy digests, fixture revision, seeds/repeats, time, cost, and result artifacts.
- [ ] Run live cases in disposable worktrees with isolated runtime state and no production credentials. Pin toolchains and bound subprocess time, network access, and spend.
- [ ] Keep static tests in ordinary CI. Run paid/live evaluations in an explicitly budgeted job; store its immutable report as admission evidence. A skipped live run cannot promote a candidate.
- [ ] Execute `framework-impeccable-ui-regression` against the baseline and candidate: keyboard flow, small-screen layout, reduced motion, and a clean incumbent design, with functional tests and human rubric assessment.

**Exit proof:** A deliberate failing fixture is detected, clean fixtures are preserved, and the report contains real executed results rather than invocation claims. Apply the spec's proposed promotion criteria only after they are ratified; publish raw counts and costs.

### Task 5: Native engine and runner compatibility

**Files:** Modify existing engine adapters/session launch integration and their contract tests only where the pilot demonstrates a gap.

- [ ] Reuse native skill discovery and invocation; do not assume a textual slash command invokes a skill on both engines.
- [ ] Add `test_framework_snapshot_survives_resume_and_rotation` and `test_framework_revocation_blocks_new_execution`.
- [ ] Run `framework-native-engine-smoke` on actual supported Claude and Codex versions, including fresh launch, resume, account rotation, and a remote runner. Verify the discovered package identity/hash and one real task result.
- [ ] Exercise rollback and the execution kill switch without disabling unrelated SDLC checks.

**Exit proof:** A compatibility matrix links live results for each supported combination; unsupported cells remain blocked rather than inferred from mocks or upstream documentation.

## PR 4 — Cross-project enforcement pilot

### Task 6: Adoption PRs, required checks, and visible status

**Files:**
- Extend: `.agents/agentctl.py` onboarding/check behavior and existing Library/SDLC API/view surfaces
- Add: a reusable framework-validation workflow using pinned validator revisions
- Modify: pilot projects through separate adoption PRs

- [ ] Write `test_framework_profile_backend_excludes_ui`, `test_framework_onboarding_preserves_local_instructions`, and `test_framework_policy_change_cannot_waive_itself`.
- [ ] Build the enforcement pilot on existing ViberPit and KinDash adoption, retain FridgeSheet as the server-rendered UI case, and add a backend-only case plus onboarding a repository without the contract. Confirm adoption is idempotent and preserves existing design context and local instructions.
- [ ] Introduce observe, evidence-required, and enforced modes per obligation. Resolve project defaults plus stack/risk overlays into a single effective snapshot.
- [ ] Configure a required merge check evaluated from trusted policy; confirm a direct/manual PR without required evidence cannot merge. Report partial enforcement if repository permissions prevent branch protection configuration.
- [ ] Show effective selection, applicability, evidence status, exceptions, and rollback through existing surfaces. Add `framework-cross-project-routing` and `framework-pilot-isolation` static evals.
- [ ] Run `python .agents/agentctl.py check` on each pilot checkout; execute one eligible and one ineligible task per profile, and a revoked-package/rollback drill.

**Exit proof:** Adoption diffs are reviewable; React changes receive UI obligations, backend-only work does not; missing evidence blocks a protected merge outside the automation loop too. Snapshot mismatch and unavailable tooling are visibly blocked.

## PR 5 — Process, architecture, performance, and security admissions

### Task 7: Admit selected candidates independently

**Files:** Candidate admission reports, selected lock entries/profiles, and named static/live cases under the existing eval layout. Split this phase into one PR per candidate so one failure cannot delay or silently approve another.

| Candidate | Required tests/evals | Promotion evidence |
|---|---|---|
| Superpowers | `framework-process-single-owner`; `framework-superpowers-debugging` | One process owner, no duplicate approval loop, observed relevant failing test followed by a verified fix, and compatibility with ViberPit's completion workflow. |
| Addy's domain skills | `framework-domain-reference-closure`; `framework-addy-api-compatibility`; `framework-addy-simplification` | Packaged shared references, passing API migration/contract tests, useful ADR tradeoffs, and behavior-preserving simplification. Generic performance and CI/CD selections need an additional measured regression/CI fixture before admission. |
| Vercel React guidance | `framework-react-stack-routing`; `framework-react-waterfall-bundle` | Measured waterfall/bundle improvement, no functional regression, and no Next.js-only API injected into a React/Vite project. |
| Trail of Bits | `framework-security-tool-allowlist`; `framework-tob-differential-review` | Detection of seeded vulnerabilities, clean-patch false-positive measurement, independent reviewer disposition, denied unapproved tools, and secret-safe outputs. |

- [ ] Complete the full vetting checklist separately for each selected revision and dependency closure.
- [ ] Establish baseline tasks and budgets before running paired evaluations. Repeat representative tasks as specified; report critical failures separately from aggregate pass rates.
- [ ] Admit only the evaluated subset. Unmeasured skills remain candidates even if siblings from the same repository are approved.
- [ ] Re-run cross-framework conflict cases after each addition and verify unchanged baseline behavior.

**Exit proof:** Each approved subset has a distinct admission decision backed by reproducible reports, live compatibility results, cost data, and a rollback pin.

## PR 6 — Compound Engineering comparison and standard releases

### Task 8: Isolated comparison pilot

**Files:** Experimental profile, admission report, and `framework-compound-comparison` behavioral cases.

- [ ] Record that Compound Engineering is not in Trail of Bits' current curated list; independently vet its actual selected revision.
- [ ] Run identical review/learning tasks under the current baseline and an isolated candidate profile. Do not enable two process owners in one session.
- [ ] Measure useful findings, repeated-defect prevention, duplicated instructions, functional outcomes, latency, and token cost. Route accepted lessons into the existing canonical learning workflow.
- [ ] Publish an adopt-selected-parts, replace-with-migration, or reject recommendation for owner decision. Do not make pilot completion equivalent to promotion.

**Exit proof:** `framework-pilot-isolation` rejects accidental standard-channel activation, and `framework-compound-comparison` produces a paired report sufficient to decide whether any adoption is worthwhile.

### Task 9: Standard channel, upgrades, revocation, and rollback

**Files:** Existing onboarding/update orchestration, Library/SDLC visibility, framework lock/profile tooling, and associated integration tests.

- [ ] Write `test_framework_upgrade_requires_new_admission`, `test_framework_revocation_invalidates_pending_acceptance`, and `test_framework_rollback_preserves_audit_history`.
- [ ] Distribute approved standard releases as project PRs with pinned validator and content revisions. Keep running sessions stable unless an explicit revocation requires blocking further execution.
- [ ] Prove new-project defaults, explicit exceptions with expiry, staged fleet migration, and rollback to the last approved version on the pilot projects.
- [ ] Retire replaced copies and instructions in the same migration; verify there is only one effective selection and one owner per concern.
- [ ] Add `framework-upgrade-and-rollback` to the static/integration corpus and schedule periodic behavioral reevaluation for engine/model changes.

**Exit proof:** An upgrade cannot silently broaden permissions or bypass admission; revocation blocks pending acceptance; rollback restores a valid profile while preserving historical evidence.

## Constitution Check and Handoff

- [ ] I: Use supported vendor surfaces; explicitly gate experimental ones.
- [ ] II: Preserve account/process isolation across local and remote runners.
- [ ] III: Attach real engine/API smoke evidence before declaring integration complete.
- [ ] IV: Verify authenticated admission/evidence, least privilege, and secret-safe artifacts.
- [ ] V: Pass required tests, lint, types, negative cases, and candidate evals.
- [ ] VI: Extend canonical contract, gates, and evals; remove replaced paths.
- [ ] VII: Record license, maintenance, security, provenance, and pinned dependencies.

Execution begins after review of the proposed policy and promotion thresholds. Deliver each phase as an independently testable PR; report measured results and remaining unsupported combinations. Merging these planning documents alone does not install frameworks, approve candidates, or change enforcement.
