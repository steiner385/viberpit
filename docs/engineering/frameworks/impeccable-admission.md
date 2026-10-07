# Impeccable admission inventory

Date: 2026-10-07. Status: **candidate / unverified; not admitted for the standard channel**.

This report records the existing installation for PR 1, Task 2 of
`docs/superpowers/plans/2026-10-07-vetted-engineering-frameworks.md`. Requirements come from
`docs/superpowers/specs/2026-10-07-vetted-engineering-frameworks.md`. Recording hashes makes
the inventory reproducible; it does not approve executing the inventoried content.

## Identity and scope

The inventory baseline is ViberPit commit `f8ae589ae6f42e5c8bf589e50b4d3fd2700de18b`;
the installation was recorded in commit `2c093aa7da3a3a057388edefc5f8c277562d2e57`.
These are **ViberPit commits**, not verified upstream Impeccable commits.

`.claude/README.md` records `pbakaus/impeccable` and installation using
`impeccable@4.1.0` with `--project --no-hooks --providers=claude,codex`.
The installed skill frontmatter identifies version **4.3.1**, and the launcher
`scripts/VERSION` identifies engine **0.1.5**. These are distinct version fields;
none establishes an immutable upstream source revision. That revision remains unknown.

The selection includes both real provider builds, `.agents/skills/impeccable/` and
`.claude/skills/impeccable/`, including their references, metadata, launchers, browser
scripts, data, and provider agent definitions. The four exported Claude agents are
`.claude/agents/impeccable-{asset-producer,documenter,finish-reviewer,manual-edit-applier}.md`.
Preserve the `provider_built` distinction in `.agents/contract.json`; the builds are
not interchangeable symlinks. The framework lock referenced by that contract is the
canonical per-file hash inventory, rather than a second hash list in this report.
The current inventory scan selected **111 vendor files** and found **zero unresolved
local Markdown link targets**. Static hashes and resolved Markdown links do not prove
that dependencies referenced dynamically by executable output are complete or vetted.

## Vetting checklist

| Requirement | Evidence and disposition |
|---|---|
| Identity and immutable provenance | Installed versions and local commits recorded above. Upstream SHA, release-to-source correspondence, and installer dependency closure are **unverified**. Do not infer them from version strings. |
| License and attribution | The installation README identifies Apache-2.0. The complete selected dependency closure, bundled browser library notices, and redistribution obligations still require independent verification. This is not a license clearance. |
| Maintainer activity and security history | No upstream maintenance/security assessment was performed for this inventory. Record dated primary-source evidence before admission. |
| Instruction supply chain | Skill setup directs execution of `scripts/impeccable context`; references expose hooks, doctor/repair, pinning, and live browser workflows. Descriptions, prompts, nested references, agent definitions, and generated output must all be reviewed for injection, credential requests, authority claims, destructive defaults, and policy conflicts. Targeted inventory is not a completed line-by-line instruction audit. |
| Executables, hooks, updates, downloads | Shell and Windows launchers, browser JavaScript, helper agents, and an external engine are present. Engine binaries are gitignored and have no independently approved artifact SHA in this admission. Automatic first-run resolution/download is not an approved executable pin; see below. Installer, engine internals, update behavior, telemetry, and transitive dependencies remain unverified. |
| Dependency completeness | Inventory both provider trees and exported agents, not just `SKILL.md`. Static integrity checks must cover packaged references and detect missing or altered files. Dynamic dependencies printed by the engine, optional browser tools, and external assets still need a complete reviewed allowlist. |
| Permissions and isolation | No new execution permissions are approved. Later sandbox trials must use disposable fixtures, isolated account/runtime homes, no production credentials, bounded subprocesses, and explicitly approved network destinations. Loading guidance does not authorize hook installation, shared-home mutation, downloads, or browser injection. |
| Native compatibility | Fresh launch, discovery/invocation, resume, account rotation, and remote runner behavior have **not been tested** against real Claude or Codex binaries in this work. Windows launcher and each engine/platform combination remain unverified. |
| Policy fit | ViberPit owns lifecycle gates, review categories, approval, completion, and canonical instructions. Impeccable can supply UI guidance, but its finish reviewer cannot replace independent repository review. Test conflicts between design directives, incumbent design context, project accessibility obligations, and completion workflow before promotion. |
| Measured value | No paired behavioral evaluation or cost measurement has run. Existing user-reported value is motivation for the pilot, not admission evidence. Apply the spec's ratified task rubric and budgets before claiming improvement. |
| Approval and maintenance | The manifest nominates repository owner `steiner385` as maintenance owner; acceptance of that responsibility is not confirmed. A separate admission approver has not been recorded. No approved execution matrix, admission decision, or known-good approved rollback package exists. Inventory authorship is not independent approval. |

## Executable and hook boundary

The inspected shell launcher resolves `IMPECCABLE_BIN`, a sibling platform binary,
an unversioned home binary, a versioned cache, a PATH executable, and finally a
GitHub release download. The unversioned home and PATH candidates undergo an
engine-probe handshake; that proves neither source identity nor an approved hash.
Override, sibling, and cached binaries can execute without a content-hash check.
The download path verifies a `.sha256` sidecar from the release channel before
execution, but a checksum fetched alongside an artifact is not an independently
approved artifact pin. `IMPECCABLE_DOWNLOAD_BASE` can also change the source.
An inventory lock must not be described as preventing these runtime resolution paths.

The launcher can use `curl` or `wget`, write to the engine cache, change executable
permissions, and spawn the engine. The installation command uses `npx`; installation
also needs its own package/dependency review. No installer, launcher, downloaded
binary, or engine probe was executed to prepare this report.

Tracked `.impeccable/config.json` has `hook.enabled: true`. This setting alone does
not establish native hook registration. No tracked native Impeccable hook registration
was found in the inventory, and there is no tracked `.codex/hooks.json`. Untracked
local or account settings were not inspected, so this report cannot claim hooks are
disabled on every host. The README's `--no-hooks` installation recipe is evidence of
the recorded recipe, not proof of current runtime state.

Browser assets include `live-browser*.js` and `modern-screenshot.umd.js`. Before
allowing live mode, review DOM/screenshot data capture, scripts injected into a page,
network destinations, server/background-process lifetime, CSP consent, and output
retention/redaction. Browser sessions may contain authenticated or private data;
tests must use synthetic fixtures. No claim of absent telemetry or secret access is
made without auditing the executable and complete dependency closure.

Project context includes `PRODUCT.md`, `DESIGN.md`, and `.impeccable/design.json`.
The README's statement that document generation has not run is partly stale relative
to the tracked design artifacts. Preserve these project-owned records; do not treat
them as immutable vendor files or overwrite them during inventory. Their presence
does not prove the workflow or native engine passed compatibility testing.

## Required evidence before promotion

1. Resolve the upstream SHA and installer provenance; independently review the complete
   selected instructions, license/notice obligations, executable artifacts, and permissions.
2. Pin each allowed executable by reviewed artifact hash and platform. Prove that
   unapproved overrides, PATH/home binaries, downloads, and hook registration cannot
   expand the approved execution boundary. Keep unknown dependencies blocked.
3. Run `framework-impeccable-projection-integrity` and `framework-lock-integrity`
   through `scripts/run_evals.py`, plus `python .agents/agentctl.py check .`, in clean
   disposable checkouts. Attach exact command results and lock digest; this report
   does not assert those runs passed. Tamper and missing-reference fixtures must fail.
4. Execute `framework-native-engine-smoke` and the paired
   `framework-impeccable-ui-regression` pilot with recorded engines, models, budgets,
   tasks, accessibility/functionality results, and human rubric judgments. Proposed
   future cases are not executed evidence; `--kind agent` listing is not a live pass.
5. Record a separate admission review, owner, date, approved permissions, supported
   compatibility cells, and rollback target. Re-vet changed hashes, references,
   executables, or permissions before any standard-channel upgrade.

The three-tier policy remains unchanged: objective integrity checks are deterministic;
fresh, independently reviewed evidence is required for promotion; aesthetic usefulness
is advisory unless the project defines a testable requirement. This inventory enables
none of the proposed new gates in `server/sdlc/gates.py`.

## Inventory verification performed

On 2026-10-07, `python3 .agents/agentctl.py check .` passed, and the static
`scripts/run_evals.py` suite passed all 20 cases, including the two framework cases.
The acceptance fixtures exercise malformed metadata, missing provenance, changed
content, missing references, unsafe paths, duplicate ownership, and incomplete
promotion declarations. These are inventory tests, not behavioral skill evaluations.

A separate disposable directory containing the actual 111 selected vendor files
and their lock passed validation. Appending content to the Claude `audit.md`
reference failed both file and projection hashes; restoring it passed; deleting
it failed the reference-closure check. No vendor code was executed by this probe.
The verified declaration digest was
`5c7515298258646ae46771ceb3d77c3d13faa7a74b4d7712dc0cd86688c6fe4a`.
Future manifest edits must rerun validation and record the new digest; these
results do not supply the missing upstream, executable, or live-engine evidence.

## Rollback and current decision

**Retain candidate/unverified status.** No upstream SHA, independently approved binary,
live compatibility report, or independent admission approval is available. PR 1 can
record and check existing files without declaring Impeccable sandbox-, pilot-, or
standard-ready. This report does not disable existing installations or enforce a
runtime execution deny; that requires the later policy/integration work.

There is no approved rollback pin yet. A ViberPit baseline commit is a reconstruction
reference only. Before promotion, choose and test a known-good approved package, or
document a reviewed withdrawal path when no earlier approved version exists. Preserve
project design records and historical evidence; change selection/lock through a
reviewed PR and verify hooks and runtime artifacts separately on affected hosts.
