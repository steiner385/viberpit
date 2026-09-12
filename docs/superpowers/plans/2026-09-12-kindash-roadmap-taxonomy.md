# KinDash Roadmap Taxonomy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn KinDash's 18 flat, all-`proposed` feature docs into a seven-parent capability tree with honest states, routing labels, one naming rule enforced by CI, and the roadmap-side links and onboarding state that make the tree true in ViberPit.

**Architecture:** One docs PR on `cairnea/KinDash` does everything the repo owns: `parent:` on every leaf, seven short parent docs whose slugs are the in-code category ids, five renames with cross-references fixed, a leading outcome sentence per doc, evidence ledgers moved to `docs/audits/feature-evidence/`, the existing `feature-doc-lint.sh` extended to enforce the convention and wired into a tier-independent CI job, a `docs/product/README.md` and ADR-023. Then, once the ccswitch nesting PR is deployed, a short run of API calls links initiatives, drains the inbox and advances onboarding.

**Tech Stack:** Markdown + YAML frontmatter · bash + bats (`.github/scripts/`) · GitHub Actions (`ci.yml`) · ccswitch HTTP API on dobby (`http://127.0.0.1:8787`).

**Spec:** `docs/superpowers/specs/2026-09-12-kindash-roadmap-5s-plan.md` (in the ccswitch repo).

## Global Constraints

- KinDash repo: `cairnea/KinDash`, default branch `main`. Work in a worktree: `git -C /mnt/ssk-ssd/tony/GitHub/KinDash fetch origin && git -C /mnt/ssk-ssd/tony/GitHub/KinDash worktree add /home/tony/GitHub/KinDash/.worktrees/docs/roadmap-taxonomy -b docs/roadmap-taxonomy origin/main`. **Never a bare top-level `cd` into it** (the ccswitch session's Bash hook is cwd-relative); use `git -C <wt>` and `(cd <wt> && …)` subshells.
- US English everywhere (ADR-020): color, behavior, organize.
- Commit style `docs(product): …` (precedent: #15702, #15806, #15825). No `Co-Authored-By` requirement in KinDash; the ccswitch session still adds `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- `ops-auto-merge.yml` arms squash auto-merge on every non-draft PR to `main`; `ci-gate` is the single required check. Open the PR non-draft only when the work is complete. Never `--force`; never bypass hooks.
- The four frontmatter keys ccswitch reads are `slug`, `title`, `state`, `parent` (plus `labels`, `links`). `owner` and `added` stay (the lint checks them); `area` goes.
- Naming rule: a leaf's slug is its title kebab-cased; a root's slug is the category id; no dates, no remediation verbs, no tool names in a capability title.
- Doc size: ≤ 300 lines hard (lint); Notes and Evidence ≤ 40 lines each; everything cut is moved verbatim, never deleted.
- Phase B (Tasks 9–11) runs only after (a) this PR has merged and pm-sync has re-indexed, and (b) the ccswitch server PR from `2026-09-12-initiative-nesting-and-roadmap-hygiene.md` is deployed on dobby.

---

## File structure (KinDash)

| Path | Responsibility |
|---|---|
| `docs/product/features/{tasks,calendar,family,ai,security,experience,delivery}.md` | the seven parents (new) |
| `docs/product/features/*.md` (18 → renamed/edited) | leaves: `parent:`, `state:`, `labels:`, outcome sentence, trimmed |
| `docs/audits/feature-evidence/<slug>.md` | ledgers and appendices moved out of the docs |
| `docs/product/README.md` | the taxonomy, naming rules, doc template, where evidence goes |
| `docs/architecture/adrs/ADR-023-product-capability-taxonomy.md` + `README.md` index row | governance |
| `.github/scripts/feature-doc-lint.sh` + `__tests__/feature-doc-lint.bats` | the convention, enforced |
| `.github/scripts/classify-changed-scope-extras.sh` + `__tests__/classify-changed-scope-extras.bats` + `__tests__/changed-scope-failopen-wiring.bats` | `feature_docs` scope output |
| `.github/workflows/ci.yml` | `feature-docs` job + rollup entry |

---

## The target tree (copy exactly)

| slug | title | parent | state | labels |
|---|---|---|---|---|
| tasks | Task management | — | live | |
| task-views | Task views | tasks | delivering | |
| task-recurrence-lifecycle | Task recurrence lifecycle | tasks | delivering | |
| calendar | Family calendar | — | live | |
| family-calendar-administration | Family calendar administration | calendar | discovery | |
| family | Family administration | — | live | |
| member-visual-identity | Member visual identity | family | live | |
| ai | AI chat assistant | — | live | |
| ai-assistant-chat | AI assistant chat | ai | delivering | |
| security | Security & privacy | — | live | |
| session-identity-integrity | Session identity integrity | security | discovery | |
| experience | Experience platform | — | delivering | |
| design-system *(was ui-framework-overhaul)* | Design system | experience | delivering | |
| accessibility *(was accessibility-verification-coverage)* | Accessibility | experience | discovery | `[accessibility]` |
| web-accessibility *(was web-accessibility-remediation)* | Web accessibility | accessibility | discovery | |
| android-accessibility *(was android-accessibility-remediation)* | Android accessibility | accessibility | discovery | |
| experience-standards *(was ui-benchmark-2026)* | Experience standards | experience | discovery | `[ui-benchmark-2026]` |
| delivery | Delivery pipeline | — | live | |
| ci-test-reliability | CI test reliability | delivery | discovery | `[ci-failure, ci-doctor]` |
| flaky-test-triage | Flaky test triage | delivery | delivering | `[flaky]` |
| deploy-health-monitoring | Deploy health monitoring | delivery | live | |
| forced-deploy-audit | Forced-deploy audit | delivery | live | `[forced-deploy-audit]` |
| merge-automation-controls | Merge automation controls | delivery | live | |
| ci-storage-and-cost-guardrails | CI storage and cost guardrails | delivery | live | |
| dependency-maintenance | Dependency maintenance | delivery | live | `[dependencies]` |

State rule (from the spec): a feature is `live` when the outcome exists; a standing bar or pipeline capability is `live` when a blocking gate or working automation enforces it, `delivering` when a remediation or build is in flight, `discovery` when it is measured or specified but nothing enforces it yet. The implementer checks each `state` above against the doc's own Evidence section and, if the evidence contradicts the table, follows the evidence and says so in the PR body.

## The outcome sentences (first paragraph after the H1, before `## Problem`)

- **tasks**: Families assign, do, approve and repeat household work from one task system whose views answer the questions a parent, teen, caregiver or child actually brings to it.
- **task-views**: Every adult and child surface reaches its highest-frequency task jobs within a small click budget, from one server-filtered query contract and one status model.
- **task-recurrence-lifecycle**: A repeating task has one rule, one live occurrence, and one terminal missed state that only a parent can reverse.
- **calendar**: One shared family calendar, synced with the providers a family already uses, that every member can read at their level.
- **family-calendar-administration**: Connecting accounts, adding calendars and choosing where a member writes is discoverable from the calendar itself and confined to the acting family.
- **family**: A family configures its members, roles and relationships in one place, and every surface honors that configuration.
- **member-visual-identity**: Every member is shown with one persisted color and one persisted pattern, resolved identically on every surface that names them.
- **ai**: A calm second pair of hands: an assistant that carries out multi-step family changes durably and reports only what the server verified.
- **ai-assistant-chat**: A multi-step assistant run is a server-owned unit of work whose outcome is verified by the server, never invented by the model or the client.
- **security**: One session shows one identity, a family's data never leaves the family without consent, and children are protected by verifiable parental controls.
- **session-identity-integrity**: The identity a shared surface displays and the credentials the browser holds always belong to the same person.
- **experience**: KinDash's web, kiosk and Android surfaces meet one published bar for accessibility, consistency and quality, and stay there without a quarterly audit to notice regressions.
- **design-system**: Every page declares one of seven page types and renders through the shared primitive for it, on web and on Android.
- **accessibility**: Every production surface has an explicit accessibility verification status, and the deterministic subset of checks blocks merge and release.
- **web-accessibility**: The web app, kiosk surfaces and admin console meet WCAG 2.2 AA on the criteria the audit tests for.
- **android-accessibility**: The Android app meets the same accessible-name, role, target-size and contrast bar the web app is held to.
- **experience-standards**: KinDash can state, from evidence in the repository, where each web and kiosk surface sits against a named published benchmark.
- **delivery**: A red signal means a real defect, a green one means the change is safe, and the pipeline's own costs and bypasses are visible and bounded.
- **ci-test-reliability**: Every test is either running and deterministic or quarantined with an owner and an expiry.
- **flaky-test-triage**: A `flaky`-labeled issue means a live, unfixed flake that a person should look at, never a stale re-detection.
- **deploy-health-monitoring**: A stalled QA deploy is visible as a stall, classified by cause, and attributed to the commit that introduced it.
- **forced-deploy-audit**: A forced production deploy stays flagged only while the shipped SHA genuinely lacks a green full-suite result.
- **merge-automation-controls**: Automation writes `main` by design, and one label is a working takeover lever on every path.
- **ci-storage-and-cost-guardrails**: Neither GitHub Actions store ever reaches its hard band, and an alert means something an operator has to do.
- **dependency-maintenance**: Renovate's signals each have a home, and the dependency dashboard is never dispatched as work.

---

### Task 1: Worktree, lint script and its tests (the convention, enforced)

**Files:**
- Modify: `.github/scripts/feature-doc-lint.sh`, `.github/scripts/__tests__/feature-doc-lint.bats`

**Interfaces:**
- Produces: the lint's new checks 7–12: `area:` forbidden; `parent:` must name an existing doc slug; a doc without `parent:` must have a slug in `ROOTS`; a non-root slug must equal kebab(title); the first non-empty line after the `# ` H1 must not start with `#` (the outcome sentence) and `## Problem` must follow it; ≤ 300 lines; `labels:` if present is a `[a, b]` flow list. `REQUIRED_KEYS=(slug title state owner added)`.

- [ ] **Step 1: Create the worktree**

```bash
git -C /mnt/ssk-ssd/tony/GitHub/KinDash fetch origin
git -C /mnt/ssk-ssd/tony/GitHub/KinDash worktree add /home/tony/GitHub/KinDash/.worktrees/docs/roadmap-taxonomy -b docs/roadmap-taxonomy origin/main
WT=/home/tony/GitHub/KinDash/.worktrees/docs/roadmap-taxonomy
```

- [ ] **Step 2: Write the failing bats tests** (append to `feature-doc-lint.bats`; also change `write_doc` so its body has an outcome line and no `area:`)

Replace `write_doc` with:

```bash
# Writes a well-formed LEAF doc under the `tasks` root; callers override fields.
write_doc() {
  local slug="$1"
  local parent="${2:-tasks}"
  local title="${3:-Some capability}"
  cat > "docs/product/features/${slug}.md" <<EOF
---
slug: ${slug}
title: ${title}
state: proposed
parent: ${parent}
owner: engineering
added: 2026-09-08
---

# ${title}

One sentence stating the outcome.

## Problem
EOF
}

write_root() {
  local slug="$1"
  cat > "docs/product/features/${slug}.md" <<EOF
---
slug: ${slug}
title: Root ${slug}
state: live
owner: engineering
added: 2026-09-12
---

# Root ${slug}

One sentence stating the outcome.

## Problem
EOF
}
```

Every existing test that calls `write_doc good-feature` must now also call `write_root tasks` first (the parent must exist); update them. Then add:

```bash
@test "area: is rejected — parent: replaced it" {
  write_root tasks
  write_doc some-capability
  sed -i 's/^parent: tasks$/area: tasks/' docs/product/features/some-capability.md
  run .github/scripts/feature-doc-lint.sh
  [ "$status" -eq 1 ]
  [[ "$output" == *"area: is not read by the indexer"* ]]
}

@test "parent: must name an existing doc" {
  write_root tasks
  write_doc some-capability nowhere
  run .github/scripts/feature-doc-lint.sh
  [ "$status" -eq 1 ]
  [[ "$output" == *"parent 'nowhere' is not a feature doc"* ]]
}

@test "a doc without parent: must be a known root" {
  write_root not-a-category
  run .github/scripts/feature-doc-lint.sh
  [ "$status" -eq 1 ]
  [[ "$output" == *"'not-a-category' has no parent and is not a root category"* ]]
}

@test "a leaf's slug is its title kebab-cased" {
  write_root tasks
  write_doc task-views tasks "Task views"
  write_doc ui-benchmark-2026 tasks "2026 web experience benchmark"
  run .github/scripts/feature-doc-lint.sh
  [ "$status" -eq 1 ]
  [[ "$output" == *"ui-benchmark-2026"*"does not equal kebab(title) '2026-web-experience-benchmark'"* ]]
  [[ "$output" != *"task-views.md::"* ]]
}

@test "the body opens with an outcome sentence before ## Problem" {
  write_root tasks
  write_doc some-capability
  sed -i '/^One sentence stating the outcome\.$/d' docs/product/features/some-capability.md
  run .github/scripts/feature-doc-lint.sh
  [ "$status" -eq 1 ]
  [[ "$output" == *"first paragraph after the H1 must be one outcome sentence"* ]]
}

@test "a doc over 300 lines fails" {
  write_root tasks
  write_doc some-capability
  yes "filler" | head -n 300 >> docs/product/features/some-capability.md
  run .github/scripts/feature-doc-lint.sh
  [ "$status" -eq 1 ]
  [[ "$output" == *"exceeds 300 lines"* ]]
}

@test "labels: must be a flow list" {
  write_root tasks
  write_doc some-capability
  sed -i 's/^state: proposed$/state: proposed\nlabels: flaky/' docs/product/features/some-capability.md
  run .github/scripts/feature-doc-lint.sh
  [ "$status" -eq 1 ]
  [[ "$output" == *"labels: must be a flow list like [flaky, ci-failure]"* ]]
  sed -i 's/^labels: flaky$/labels: [flaky]/' docs/product/features/some-capability.md
  run .github/scripts/feature-doc-lint.sh
  [ "$status" -eq 0 ]
}
```

- [ ] **Step 3: Run to verify they fail**

Run: `(cd $WT && bats .github/scripts/__tests__/feature-doc-lint.bats)`
Expected: the new tests FAIL; the live-tree test FAILS too (the 18 docs still carry `area:`), which is expected until Task 3.

- [ ] **Step 4: Implement the lint**

In `feature-doc-lint.sh`: header comment gains checks 7–12 and the "why": *the indexer reads only slug/title/state/parent/labels/links; `area:` was a second taxonomy nobody read (18 docs, 11 values, 2026-09-12 review)*. Then:

```bash
REQUIRED_KEYS=(slug title state owner added)
# Top-level capabilities are the in-code feature categories
# (server/config/feature-gates.ts FEATURE_CATEGORIES) plus the two standing
# bars and the delivery pipeline (ADR-023). A doc with no parent: must be one.
ROOTS=(core ai financial tasks family content goals integrations gamification
       notifications calendar communication security experience delivery)
MAX_LINES=300

kebab() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/&/and/g; s/[^a-z0-9]+/-/g; s/^-+//; s/-+$//'; }
is_root() { local s; for s in "${ROOTS[@]}"; do [ "$s" = "$1" ] && return 0; done; return 1; }
```

First pass (before the main loop): collect every doc's slug into `all_slugs` (an associative array) so `parent:` can be validated against docs later in the listing. In the main loop after check 6:

```bash
  # --- 7. area: is dead weight — parent: is the grouping field --------------
  if printf '%s\n' "$fm" | grep -qE '^area:'; then
    echo "::error file=$rel::feature-doc-lint: area: is not read by the indexer (only slug/title/state/parent/labels/links are); express the grouping with parent:"
    fail=1
  fi

  # --- 8/9. parent: names a doc; a root is a known category -----------------
  parent="$(printf '%s\n' "$fm" | sed -n 's/^parent:[[:space:]]*//p' | head -n 1)"
  parent="${parent%"${parent##*[![:space:]]}"}"
  if [ -n "$parent" ]; then
    if [ -z "${all_slugs[$parent]:-}" ]; then
      echo "::error file=$rel::feature-doc-lint: parent '$parent' is not a feature doc in $DOCS_DIR"
      fail=1
    fi
  elif ! is_root "$slug"; then
    echo "::error file=$rel::feature-doc-lint: '$slug' has no parent and is not a root category (${ROOTS[*]})"
    fail=1
  fi

  # --- 10. a leaf's slug is its title, kebab-cased ---------------------------
  title="$(printf '%s\n' "$fm" | sed -n 's/^title:[[:space:]]*//p' | head -n 1)"
  title="${title%"${title##*[![:space:]]}"}"
  if [ -n "$parent" ] && [ "$(kebab "$title")" != "$slug" ]; then
    echo "::error file=$rel::feature-doc-lint: slug '$slug' does not equal kebab(title) '$(kebab "$title")' — rename one so the roadmap and the file agree"
    fail=1
  fi

  # --- 11. outcome sentence, then ## Problem ---------------------------------
  first_body="$(awk '/^---$/{n++; next} n>=2 && /^# /{h=1; next} n>=2 && h && NF{print; exit}' "$doc")"
  if [ -z "$first_body" ] || printf '%s' "$first_body" | grep -qE '^#'; then
    echo "::error file=$rel::feature-doc-lint: the first paragraph after the H1 must be one outcome sentence (the roadmap shows it as the summary); ## Problem comes after it"
    fail=1
  fi

  # --- 12. size and labels shape --------------------------------------------
  lines="$(wc -l < "$doc")"
  if [ "$lines" -gt "$MAX_LINES" ]; then
    echo "::error file=$rel::feature-doc-lint: $lines lines exceeds $MAX_LINES — move ledgers and appendices to docs/audits/feature-evidence/$slug.md"
    fail=1
  fi
  labels_line="$(printf '%s\n' "$fm" | grep -E '^labels:' | head -n 1 || true)"
  if [ -n "$labels_line" ] && ! printf '%s' "$labels_line" | grep -qE '^labels:[[:space:]]*\[[^]]*\][[:space:]]*$'; then
    echo "::error file=$rel::feature-doc-lint: labels: must be a flow list like [flaky, ci-failure]"
    fail=1
  fi
```

Update the success line to `… conform to the slug/title/state/parent convention.` and the live-tree bats assertion string to match.

- [ ] **Step 5: Run the bats file** (fixture tests must pass; the live-tree test still fails until Task 3): `(cd $WT && bats .github/scripts/__tests__/feature-doc-lint.bats)`.

- [ ] **Step 6: Commit**

```bash
git -C $WT add .github/scripts/feature-doc-lint.sh .github/scripts/__tests__/feature-doc-lint.bats
git -C $WT commit -F /tmp/kd-c1.txt   # "ci(docs): feature-doc lint enforces parent, roots, kebab slugs, outcome sentence, size, labels"
```

---

### Task 2: The `feature-docs` CI job runs on docs-tier PRs

**Files:**
- Modify: `.github/scripts/classify-changed-scope-extras.sh`, `.github/scripts/__tests__/classify-changed-scope-extras.bats`, `.github/scripts/__tests__/changed-scope-failopen-wiring.bats`, `.github/workflows/ci.yml` (changed-scope outputs, new job after `fix-patterns`, rollup `needs:` and `CONDITIONAL_RESULTS`)

**Interfaces:**
- Produces: scope output `feature_docs` (true iff the diff touches `docs/product/features/` or `.github/scripts/feature-doc-lint.sh`; true on fail-open and `ci:full`); job `feature-docs` (bash only, no toolchain) running `.github/scripts/feature-doc-lint.sh .`.

- [ ] **Step 1: Write the failing bats tests** — in `classify-changed-scope-extras.bats` add, mirroring the `fix_patterns` block:

```bash
# ── feature_docs (roadmap taxonomy, 2026-09-12) ─────────────────────────────
@test "feature_docs: a feature doc change => true" {
  run_classifier "docs/product/features/task-views.md"
  [ "$(get feature_docs)" = "true" ]
}

@test "feature_docs: the lint script itself => true" {
  run_classifier ".github/scripts/feature-doc-lint.sh"
  [ "$(get feature_docs)" = "true" ]
}

@test "feature_docs: an unrelated docs page => false" {
  run_classifier "docs/README.md"
  [ "$(get feature_docs)" = "false" ]
}
```

(`run_classifier`/`get` are the file's own helpers — read lines 1–18 and use their exact names.) Add `[ "$(get feature_docs)" = "true" ]` to the two fail-open tests. In `changed-scope-failopen-wiring.bats`, add `feature_docs` next to every `fix_patterns` assertion (the "six new outputs" comment becomes seven).

- [ ] **Step 2: Run to verify they fail**: `(cd $WT && bats .github/scripts/__tests__/classify-changed-scope-extras.bats .github/scripts/__tests__/changed-scope-failopen-wiring.bats)`.

- [ ] **Step 3: Implement**

`classify-changed-scope-extras.sh`: add `echo "feature_docs=true"` to `fail_open`, and

```bash
feature_docs=false
if printf '%s\n' "$NONBLANK" | grep -qE '^(docs/product/features/|\.github/scripts/feature-doc-lint\.sh$)'; then
  feature_docs=true
fi
```

plus `echo "feature_docs=$feature_docs"` at the end; document it in the header beside `fix_patterns`.

`ci.yml`: (1) in `changed-scope.outputs` add `feature_docs: ${{ steps.scope.outputs.feature_docs }}` with a comment; (2) wherever the workflow normalizes/defaults the extras outputs (grep `fix_patterns` in ci.yml — every site), add `feature_docs` the same way; (3) add the job right after `fix-patterns`:

```yaml
  feature-docs:
    name: "docs: feature docs conform (ADR-023)"
    needs: [changed-scope]
    if: >-
      (github.event_name != 'pull_request' ||
       github.event.pull_request.draft == false ||
       contains(github.event.pull_request.labels.*.name, 'ci:full')) &&
      (github.event_name != 'pull_request' ||
       needs.changed-scope.outputs.feature_docs == 'true' ||
       contains(github.event.pull_request.labels.*.name, 'ci:full'))
    # Tier-independent on purpose: docs/product/features/** is docs-tier, and
    # bats-tests (which also runs this lint via feature-doc-lint.bats) skips on
    # the docs tier — so without this job a nonconformant doc lands on main and
    # pm-sync silently drops it from the roadmap. Bash only: no toolchain.
    runs-on: ${{ vars.CI_RUNNER_TIER == 'arc' && 'kindash-arc' || 'ubuntu-latest' }}
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10  # v6.0.3
        with:
          fetch-depth: 1
      - name: Lint feature docs
        run: .github/scripts/feature-doc-lint.sh .
```

(4) in the `ci` rollup: add `- feature-docs` to `needs:` beside `fix-patterns`, and `feature-docs=${{ needs.feature-docs.result }}` to `CONDITIONAL_RESULTS`.

- [ ] **Step 4: Run the CI guards locally**: `(cd $WT && bats .github/scripts/__tests__/classify-changed-scope-extras.bats .github/scripts/__tests__/changed-scope-failopen-wiring.bats .github/scripts/__tests__/check-ci-rollup-tiers.bats)` → PASS.

- [ ] **Step 5: Commit**

```bash
git -C $WT add .github/scripts/classify-changed-scope-extras.sh .github/scripts/__tests__/ .github/workflows/ci.yml
git -C $WT commit -F /tmp/kd-c2.txt   # "ci: feature-docs job lints docs/product/features on every PR that touches it"
```

---

### Task 3: Seven parent docs

**Files:**
- Create: `docs/product/features/{tasks,calendar,family,ai,security,experience,delivery}.md`

- [ ] **Step 1: Write each parent** from this template, substituting the table row and the outcome sentence above. Body sections stay short (≤ 60 lines total); Evidence names the code/docs that prove the area exists.

```markdown
---
slug: tasks
title: Task management
state: live
owner: engineering
added: 2026-09-12
---

# Task management

Families assign, do, approve and repeat household work from one task system whose views answer the questions a parent, teen, caregiver or child actually brings to it.

## Problem

<2–4 sentences: what this area is for and what the children below it each own.>

## Target users

<bullets, drawn from the children's Target users.>

## Desired outcome

<the outcome sentence, expanded to 3–5 sentences; name the children as the parts.>

## Success metrics

<roll-up of the children's metrics, or "(unknown — needs owner input)".>

## Non-goals

<what lives elsewhere: e.g. for `tasks`, calendar-driven scheduling is `calendar`; rewards money is `financial` when it gets a doc.>

## Notes

Children: `task-views`, `task-recurrence-lifecycle`. Category id `tasks` matches `FEATURE_CATEGORIES` in `server/config/feature-gates.ts`; the topic label `tasks` on GitHub names the same area.

## Evidence

- `server/config/feature-gates.ts` — the `tasks` category and its gates
- `services/task-management-service/` — the task engine
- `docs/KinDash-Overview.md` § "2. Task Management"
```

Per-parent evidence pointers: **calendar** → `frontend/src/features/calendar/`, overview § "3. Family Calendar", `docs/superpowers/specs/2026-04-28-family-calendar-management-design.md`; **family** → ADR-015, ADR-022, `docs/FAMILY_ISOLATION_GUIDE.md`, overview § "10. Family Administration"; **ai** → overview § "7. AI Chat Assistant", `docs/help/content/ai_assistant.md`, `docs/architecture/AI_ARCHITECTURE.md`; **security** → ADR-014, ADR-016, ADR-022, `docs/COPPA_COMPLIANCE.md`, `docs/FAMILY_ISOLATION_GUIDE.md`, `docs/audits/COPPA-SDK-AUDIT.md`; **experience** → `docs/superpowers/specs/2026-04-24-neurodivergent-design-principles-design.md`, the Storybook axe ratchet (`_accessibility-tests.yml`), `docs/audits/task-views-ux-benchmark-2026-09-07.md`; **delivery** → `.github/workflows/ci.yml`, `ops-ci-gate.yml`, `ops-auto-merge.yml`, `deploy-production.yml`, `docs/DEPLOYMENT-STRATEGY.md`.

`security`'s Notes must say: "Future leaves as their docs arrive: `coppa-parental-consent` (ADR-014), `family-isolation` (ADR-022), `step-up-auth` (ADR-016)." `experience`'s and `delivery`'s Notes must say they are not `FEATURE_CATEGORIES` ids and why (a standing bar and the pipeline are not sold features).

- [ ] **Step 2: Lint**: `(cd $WT && .github/scripts/feature-doc-lint.sh .)` — parents pass their own checks (the 18 leaves still fail until Task 4).

- [ ] **Step 3: Commit**

```bash
git -C $WT add docs/product/features/{tasks,calendar,family,ai,security,experience,delivery}.md
git -C $WT commit -F /tmp/kd-c3.txt   # "docs(product): seven parent capabilities anchored on the feature categories (ADR-023)"
```

---

### Task 4: Renames and cross-references

**Files:**
- Rename (git mv): `ui-benchmark-2026.md → experience-standards.md`, `ui-framework-overhaul.md → design-system.md`, `accessibility-verification-coverage.md → accessibility.md`, `web-accessibility-remediation.md → web-accessibility.md`, `android-accessibility-remediation.md → android-accessibility.md`

- [ ] **Step 1: Move**

```bash
(cd $WT/docs/product/features && git mv ui-benchmark-2026.md experience-standards.md && git mv ui-framework-overhaul.md design-system.md && git mv accessibility-verification-coverage.md accessibility.md && git mv web-accessibility-remediation.md web-accessibility.md && git mv android-accessibility-remediation.md android-accessibility.md)
```

- [ ] **Step 2: Fix every cross-reference inside the directory** (both the bare-slug and `.md` forms; the exploration listed every site):

```bash
(cd $WT/docs/product/features && sed -i \
  -e 's/accessibility-verification-coverage/accessibility/g' \
  -e 's/web-accessibility-remediation/web-accessibility/g' \
  -e 's/android-accessibility-remediation/android-accessibility/g' \
  -e 's/ui-framework-overhaul/design-system/g' \
  -e 's/`ui-benchmark-2026`\.md/`experience-standards`.md/g; s/ui-benchmark-2026\.md/experience-standards.md/g' *.md)
```

Then **restore the label mentions**: `experience-standards.md` describes `ui-benchmark-2026` as a live GitHub label (formerly lines 20 and 370); those two sentences must keep the label name. Read the file after the sed and put back the label wherever the sentence is about the label, not the doc.

- [ ] **Step 3: Update the two outside references**: `docs/modules/task-management-summary.md:8` and `docs/superpowers/plans/2026-09-10-recurrence-train-2-repeat-fold.md:740` mention `task-recurrence-lifecycle.md`, which does not move — no edit needed; confirm with `git -C $WT grep -n "docs/product/features" -- ':!docs/product/features'`.

- [ ] **Step 4: Commit**

```bash
git -C $WT add -A docs/product/features
git -C $WT commit -F /tmp/kd-c4.txt   # "docs(product): rename five program-shaped docs to standing capabilities"
```

---

### Task 5: Frontmatter, outcome sentences, states and labels on all 18 leaves

**Files:**
- Modify: every leaf under `docs/product/features/` per the target tree.

- [ ] **Step 1: For each leaf**, edit the frontmatter to exactly (keep `owner`/`added`; drop `area`; add `parent`; set `state`; add `labels` where the table gives one; `title` per the table):

```yaml
---
slug: task-views
title: Task views
state: delivering
parent: tasks
owner: engineering
added: 2026-09-08
---
```

and insert the outcome sentence as the first paragraph after the H1 (rename the H1 for the five renamed docs). A scripted pass is fine for frontmatter; the outcome sentences are pasted from the list above.

- [ ] **Step 2: Lint**: `(cd $WT && .github/scripts/feature-doc-lint.sh .)` → only "exceeds 300 lines" errors may remain (handled in Task 6).

- [ ] **Step 3: Commit**

```bash
git -C $WT add docs/product/features
git -C $WT commit -F /tmp/kd-c5.txt   # "docs(product): every leaf declares its parent, state, routing labels and outcome sentence"
```

---

### Task 6: Move ledgers to `docs/audits/feature-evidence/`

**Files:**
- Create: `docs/audits/feature-evidence/<slug>.md` for every doc over 300 lines (at least: ci-test-reliability 1096, design-system 1082, forced-deploy-audit 1001, web-accessibility 723, flaky-test-triage 651, task-recurrence-lifecycle 594, deploy-health-monitoring 588, accessibility 558, experience-standards 473, android-accessibility 468, task-views 454, ai-assistant-chat 427, family-calendar-administration 365)

- [ ] **Step 1: For each such doc**, keep Problem / Target users / Desired outcome / Success metrics / Non-goals in full. Cut `## Notes` to ≤ 40 lines and `## Evidence` to ≤ 40 lines: keep the representative file paths and issue/PR numbers; move the rest (dated re-verification stamps, per-item ledgers, appendices, long tables) **verbatim** into `docs/audits/feature-evidence/<slug>.md` under:

```markdown
# <Title> — evidence ledger

Moved out of `docs/product/features/<slug>.md` on 2026-09-12 (roadmap taxonomy, ADR-023). The feature doc is the outcome contract; this file is the audit trail. Append new re-verification stamps here.

## Notes (moved)

…

## Evidence (moved)

…
```

and end the doc's Evidence with `Full ledger: docs/audits/feature-evidence/<slug>.md`.

- [ ] **Step 2: Lint until clean**: `(cd $WT && .github/scripts/feature-doc-lint.sh . && bats .github/scripts/__tests__/feature-doc-lint.bats)` → PASS including the live-tree test.

- [ ] **Step 3: Commit**

```bash
git -C $WT add docs/product/features docs/audits/feature-evidence
git -C $WT commit -F /tmp/kd-c6.txt   # "docs(product): feature docs are outcome contracts; ledgers move to docs/audits/feature-evidence"
```

---

### Task 7: `docs/product/README.md` and ADR-023

**Files:**
- Create: `docs/product/README.md`, `docs/architecture/adrs/ADR-023-product-capability-taxonomy.md`
- Modify: `docs/architecture/adrs/README.md` (index row)

- [ ] **Step 1: README** — contents, in this order: (1) what the roadmap is (product → capability → sub-capability; initiatives are GitHub epics adopted by ViberPit; issues are leaves); (2) the tree, pasted from the target table as a code block; (3) the three kinds of parent (feature area / standing bar / delivery system) with the state rule; (4) naming rules (bulleted, from Global Constraints); (5) the doc template (frontmatter with all six keys, outcome sentence, the seven sections, size caps, where evidence goes); (6) `labels:` — what it routes and the rule that one label routes to one doc; (7) how a new capability lands (a PR that adds the doc; ViberPit indexes it on the next sync; the lint gates it); (8) links: ADR-023, `server/config/feature-gates.ts`, `.github/scripts/feature-doc-lint.sh`.

- [ ] **Step 2: ADR-023** in the ADR-021 format:

```markdown
# ADR-023: One product capability taxonomy, anchored on the feature categories

**Status:** Accepted
**Date:** 2026-09-12
**Deciders:** Tony Stein (owner)
**Related:** `docs/product/README.md`, `server/config/feature-gates.ts` (`FEATURE_CATEGORIES`),
`.github/scripts/feature-doc-lint.sh`, ViberPit roadmap 5S plan (2026-09-12), ADR-020

## Context

<the four competing taxonomies, the flat 18-doc map, the three kinds of thing at one level, the naming drift — 2–3 paragraphs from the spec's "root causes".>

## Decision

1. Top-level capabilities are the `FEATURE_CATEGORIES` ids plus `security`, `experience` and `delivery`. Display titles use the product's marketing names.
2. Every other capability declares exactly one `parent:`. `area:` is retired.
3. Three kinds of parent, told apart by whose experience they describe and what `live` means (table).
4. Naming: a leaf's slug is its title kebab-cased; no dates, remediation verbs or tool names.
5. A doc opens with one outcome sentence; Notes/Evidence ≤ 40 lines; ledgers live in `docs/audits/feature-evidence/`.
6. `labels:` routes bot-filed and audit-filed issues mechanically; one label → one doc.
7. The lint in CI is the gate; a doc that fails it is not on the roadmap.

## Alternatives considered

- Marketing names as slugs (rejected: three surfaces already key on the category ids).
- Keeping delivery work out of the map (rejected: one repo is one product in ViberPit, and the triage loop needs a home for bot-filed issues).
- Filtering program epics instead of nesting (rejected in ViberPit: hides data; nesting is mirrored).

## Consequences

<what changes for authors and agents; what the lint now refuses; what ViberPit reads.>

## Revisit triggers

- A `FEATURE_CATEGORIES` change (the roots list in the lint must follow).
- A parent with one child for more than a quarter (fold or wait).
- More than three levels in use (the drill-down UI assumes shallow trees).
```

Add the index row to `docs/architecture/adrs/README.md`.

- [ ] **Step 3: US-English sweep**: `(cd $WT && pnpm lint:us-english:docs)` → clean (fix any British spellings it reports in the new files).

- [ ] **Step 4: Commit**

```bash
git -C $WT add docs/product/README.md docs/architecture/adrs/ADR-023-product-capability-taxonomy.md docs/architecture/adrs/README.md
git -C $WT commit -F /tmp/kd-c7.txt   # "docs(product): README for the capability taxonomy; ADR-023"
```

---

### Task 8: The KinDash PR

- [ ] **Step 1: Verify** locally: `(cd $WT && .github/scripts/feature-doc-lint.sh . && bash scripts/run-bats.sh)` → all bats green.
- [ ] **Step 2: In-flight check**: open PRs on `cairnea/KinDash` touching `docs/product/` or the lint (GraphQL as in `core.md`, owner `cairnea`, name `KinDash`). Coordinate if any.
- [ ] **Step 3: Push and open the PR** non-draft, title `docs(product): capability taxonomy — parents, renames, states, labels, README, ADR-023, lint gate`, body from a file: summary, the target tree, the state decisions with evidence, the renames, the moved ledgers, "Type of change: documentation + CI", test plan (bats + lint), and a note that the roadmap side (initiative links, onboarding advance) follows in ViberPit. Auto-merge is armed by `ops-auto-merge.yml`; if the tier classifier runs the full suite (ci.yml changed), let it.
- [ ] **Step 4: Watch `ci-gate`** to green and merge; then confirm pm-sync re-indexed: `curl -s http://127.0.0.1:8787/api/pm/map | python3 -c "import json,sys; p=[x for x in json.load(sys.stdin)['products'] if x['slug']=='kindash'][0]; print(p['counts']['capabilities'], sorted(c['slug'] for c in p['capabilities']))"` → 25 capabilities, 7 roots.

---

### Task 9 (Phase B): Onboarding state and initiative titles

Preconditions: Task 8 merged and indexed; the ccswitch server PR deployed (`deploy/status` shows the new SHA).

- [ ] **Step 1: Sync** `curl -s -X POST http://127.0.0.1:8787/api/pm/projects/kindash/sync`, wait a tick, then `curl -s http://127.0.0.1:8787/api/pm/projects/kindash | python3 -c "import json,sys; print(json.load(sys.stdin)['onboardingState'])"` → `map_merged` (Task 6 of the ccswitch plan).
- [ ] **Step 2: Advance to live** `curl -s -X POST http://127.0.0.1:8787/api/pm/projects/kindash/advance-live` → `{"onboardingState": "live"}`.
- [ ] **Step 3: Confirm titles and nesting** `curl -s http://127.0.0.1:8787/api/sdlc/initiatives | python3 -c "import json,sys; [print(i['slug'], '|', i['title'], '| parent=', i['parentInitiativeSlug'], '| stage=', i['stage']) for i in json.load(sys.stdin) if i['repo']=='cairnea/KinDash']"` → no `epic(` prefixes; the five ui-audit phases and four web-benchmark phases carry their program as parent.

### Task 10 (Phase B): Link initiatives to capabilities

- [ ] **Step 1: Feature ids** from the map: `curl -s http://127.0.0.1:8787/api/pm/map | python3 -c "import json,sys; p=[x for x in json.load(sys.stdin)['products'] if x['slug']=='kindash'][0]; walk=lambda cs: [print(c['slug'], c['id']) or walk(c['children']) for c in cs]; walk(p['capabilities'])"`.
- [ ] **Step 2: PATCH each initiative** (`primaryCapabilityId` + `capabilityIds`), mapping by epic number:

| epic | primary capability |
|---|---|
| 15137, 15138, 15139 | design-system |
| 15474 (program), 15479 (05) | accessibility |
| 15475 (01), 15476 (02), 15478 (04) | web-accessibility |
| 15477 (03) | android-accessibility |
| 15549 (program), 15550–15553 | experience-standards |
| 15727 | task-views |

```bash
patch() { curl -s -X PATCH "http://127.0.0.1:8787/api/sdlc/initiatives/$1" -H 'Content-Type: application/json' -d "{\"primaryCapabilityId\":\"$2\",\"capabilityIds\":[\"$2\"]}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['slug'], '->', d['primaryCapability'] and d['primaryCapability']['slug'])"; }
```

(Look up each current slug from Step 3 of Task 9; slugs did not change with the title normalization.)

- [ ] **Step 3: Verify** the map: every KinDash initiative has a `primaryCapability`; the capability pages show them nested; `curl -s http://127.0.0.1:8787/api/pm/projects/kindash/health` → `"unlinkedInitiatives": []`.

### Task 11 (Phase B): Drain the inbox

- [ ] **Step 1: List** `curl -s 'http://127.0.0.1:8787/api/pm/triage?projectSlug=kindash'` and review each proposal's `proposedFeatureSlug` against the doc it names (the 2026-09-12 snapshot: 12 flaky → flaky-test-triage, 2 forced-deploy → forced-deploy-audit, 4 → task-recurrence-lifecycle, 2 → ci-test-reliability, 1 `no_fit` marketing-site-content).
- [ ] **Step 2: Accept** the sound ones in one call: `curl -s -X POST http://127.0.0.1:8787/api/pm/triage/accept-bulk -H 'Content-Type: application/json' -d '{"ids": [...]}'`; **reject** the marketing item (`POST /api/pm/triage/<id>/reject`) — it belongs to the `web` product and stays `needs_human` until that product's map lands.
- [ ] **Step 3: Verify** `curl -s http://127.0.0.1:8787/api/pm/projects/kindash/health` → `staleInbox: 0`, `staleProposed: []`, `unparentedCapabilities: []`; the Roadmap page shows KinDash with seven roots and every initiative under a capability.
- [ ] **Step 4: Record** the outcome in the ccswitch spec's "Decisions" section (one line: applied 2026-09-XX, PR numbers) and commit it with the ccswitch web PR.

---

## Self-review

- **Spec coverage.** Sort (verdicts, ledgers, labels) → Tasks 4–6 and 11; Set in order (tree, naming, frontmatter, size) → Tasks 1, 3–5; Shine (states → Task 5; links → Task 10; inbox → Task 11; onboarding → Task 9; trim → Task 6); Standardize (README, ADR-023, lint) → Tasks 1–2, 7; Sustain (labels retired) — **not in this plan**: label retirement is a separate sweep with `server/pm/label_retirement.py` after the tree lands, tracked as a follow-up in the PR body.
- **Placeholder scan.** Parent-doc bodies are templated with explicit per-parent evidence; the outcome sentences are written out; the lint code is complete; the CI job is complete. The only "read the file and reuse its helper" notes name the file and line (`classify-changed-scope-extras.bats` helpers).
- **Consistency.** Slugs/titles/parents/states/labels appear once (the target table) and every task points at it; the lint's `ROOTS` list equals the table's roots plus the remaining `FEATURE_CATEGORIES` ids; the ccswitch plan's `labels:` parsing (its Task 7) accepts exactly the flow-list form the lint requires.
