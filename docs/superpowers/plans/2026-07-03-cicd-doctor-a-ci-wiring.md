# CI/CD Doctor — Plan A: CI Wiring (JUnit emission, advisory e2e, trigger dedupe)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make CI emit machine-readable (JUnit XML) test results, add the Playwright e2e suite as an advisory (non-required) job, and stop the push+pull_request double-run — the prerequisite sensing surface for the CI/CD Doctor (spec: `docs/superpowers/specs/2026-07-03-cicd-doctor-design.md`).

**Architecture:** All changes are GitHub Actions YAML (`.github/workflows/ci.yml`) plus one Playwright config edit. No server/web application code changes. Each job uploads its JUnit XML as an artifact named with the `junit-` prefix — the naming contract the Doctor's ingestor (Plan C) relies on.

**Tech Stack:** GitHub Actions, pytest `--junitxml`, vitest junit reporter, Playwright junit reporter + `webServer`.

## Global Constraints

- Artifact naming contract: every JUnit artifact name starts with `junit-` (Plan C's ingestor filters on this prefix). Do not deviate.
- The `e2e` job must NOT be added to branch protection required checks — it is advisory by design; the Doctor proposes graduation later.
- Required check names `lint-and-test` and `frontend` must not be renamed (branch protection references them by name).
- Local verification commands: use system `python3` with `PYTHONPATH=.:src` and `CCSWITCH_DEV=1` (no prebuilt venv exists in fresh worktrees); web commands need `npm ci` in `web/` first.
- Completion workflow (CLAUDE.md): branch off `origin/master` (NEVER local `master`), push, open a non-draft PR, `gh pr merge <#> --squash --auto`.

---

### Task 0: Branch setup

**Files:** none (git only)

- [ ] **Step 1: Create the working branch off origin/master**

```bash
git fetch origin
git checkout -b feat/ci-junit-wiring origin/master
```

Expected: new branch `feat/ci-junit-wiring` tracking `origin/master`'s tip.

---

### Task 1: Deduplicate triggers + add concurrency cancellation

**Files:**
- Modify: `.github/workflows/ci.yml` (top-level `on:` block; add `concurrency:`)

**Interfaces:**
- Produces: `on.push.branches == [master]`, `on.pull_request` unchanged, a top-level `concurrency` group. Tasks 2–3 edit other parts of the same file and must preserve this block.

Today `on: push` + `on: pull_request` runs the full matrix twice for every PR-branch push. After this task: pushes only build on `master`; PR branches build once via the `pull_request` event; superseded PR pushes cancel the in-flight run (but `master` runs are never cancelled).

- [ ] **Step 1: Edit the trigger and add concurrency**

Replace the current top of `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
  pull_request:
```

with:

```yaml
name: CI

on:
  push:
    branches: [master]
  pull_request:

# One in-flight run per ref: a superseded PR push cancels the stale run.
# master runs are never cancelled (deploys watch them).
concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: ${{ github.ref != 'refs/heads/master' }}
```

- [ ] **Step 2: Validate the YAML parses and encodes the intent**

```bash
python3 - <<'EOF'
import yaml
d = yaml.safe_load(open(".github/workflows/ci.yml"))
on = d.get("on", d.get(True))  # YAML 1.1 parses bare `on:` as boolean True
assert on["push"]["branches"] == ["master"], on
assert "pull_request" in on, on
assert "concurrency" in d and d["concurrency"]["group"].startswith("ci-"), d.get("concurrency")
print("ok")
EOF
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: dedupe push/pull_request double-run + cancel superseded PR runs"
```

---

### Task 2: JUnit emission for the Python suites + pip cache

**Files:**
- Modify: `.github/workflows/ci.yml` (the `lint-and-test` job)

**Interfaces:**
- Produces: artifact `junit-python` containing `test-results/core.xml` and `test-results/server.xml` on every run (even failing ones — `if: always()`).

The core suite currently runs via `python -m unittest discover -s tests`, which cannot emit JUnit. pytest natively collects `unittest.TestCase` classes, so both suites become pytest invocations with `--junitxml`.

- [ ] **Step 1: Verify locally that pytest collects the core unittest suite**

```bash
PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest tests/ -q --collect-only 2>&1 | tail -3
```

Expected: a positive `N tests collected` count, no collection errors. (If collection errors appear, STOP and report — the fold-in assumption is wrong and the step needs a human decision.)

- [ ] **Step 2: Verify JUnit emission works on one stable test file**

```bash
PYTHONPATH=.:src CCSWITCH_DEV=1 python3 -m pytest server/tests/test_automation_models.py -q \
  --junitxml=/tmp/junit-check.xml && python3 -c "
from xml.etree import ElementTree
r = ElementTree.parse('/tmp/junit-check.xml').getroot()
assert list(r.iter('testcase')), 'no testcases in junit xml'
print('ok')"
```

Expected: tests pass and `ok`. (Full local `server/tests/` runs are noisy — daemon socket / X11 / OAuth failures are environmental, not regressions; CI is the arbiter.)

- [ ] **Step 3: Edit the lint-and-test job**

In `.github/workflows/ci.yml`, replace:

```yaml
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install deps
        run: pip install -e ".[server]"
      - name: Ruff check
        run: ruff check src/ server/
      - name: Mypy
        run: mypy server/ --ignore-missing-imports
      - name: Core unit tests
        run: python -m unittest discover -s tests
      - name: Server tests
        run: pytest server/tests/ -q
```

with:

```yaml
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: pyproject.toml
      - name: Install deps
        run: pip install -e ".[server]"
      - name: Ruff check
        run: ruff check src/ server/
      - name: Mypy
        run: mypy server/ --ignore-missing-imports
      - name: Core unit tests
        run: python -m pytest tests/ -q --junitxml=test-results/core.xml
      - name: Server tests
        run: python -m pytest server/tests/ -q --junitxml=test-results/server.xml
      - name: Upload test results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: junit-python
          path: test-results/*.xml
          retention-days: 30
```

- [ ] **Step 4: Validate YAML**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml')); print('ok')"
```

Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: emit JUnit XML from python suites, cache pip (CI/CD Doctor sensing)"
```

---

### Task 3: Frontend JUnit + advisory e2e job

**Files:**
- Modify: `.github/workflows/ci.yml` (the `frontend` job; append a new `e2e` job)
- Modify: `web/playwright.config.ts` (reporter + webServer)

**Interfaces:**
- Produces: artifacts `junit-vitest` (from `frontend`) and `junit-e2e` (from `e2e`). The `e2e` job name is the status-check context the Doctor will track as its first graduation candidate.

- [ ] **Step 1: Enable Playwright's junit reporter and webServer**

In `web/playwright.config.ts`, replace:

```ts
  reporter: 'html',
```

with:

```ts
  // CI: machine-readable results for the CI/CD Doctor + terse console output.
  reporter: process.env.CI
    ? [['list'], ['junit', { outputFile: 'test-results/e2e.xml' }]]
    : 'html',
```

and replace the commented-out block:

```ts
  // webServer: {
  //   command: 'npm run preview',
  //   url: 'http://127.0.0.1:4173',
  //   reuseExistingServer: !process.env.CI,
  // },
```

with:

```ts
  webServer: {
    command: 'npm run preview',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI,
  },
```

- [ ] **Step 2: Verify the Playwright config loads and lists the specs**

```bash
cd web && npm ci --no-audit --no-fund >/dev/null && npx playwright test --list 2>&1 | tail -5
```

Expected: the three specs listed (`dashboard`, `transcript-virtualization`, `rotation-reconnect`), no config errors. (Symlinking `node_modules` from the main checkout also works if `npm ci` is blocked.)

- [ ] **Step 3: Add vitest JUnit output to the frontend job**

In `.github/workflows/ci.yml`, in the `frontend` job, replace:

```yaml
      - name: Unit tests
        run: npm run test
```

with:

```yaml
      - name: Unit tests
        run: npm run test -- --reporter=default --reporter=junit --outputFile=test-results/vitest.xml
      - name: Upload test results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: junit-vitest
          path: web/test-results/*.xml
          retention-days: 30
```

(Note: `upload-artifact` paths are workspace-relative, hence `web/test-results` despite the job's `working-directory: web` default for `run` steps.)

- [ ] **Step 4: Append the advisory e2e job**

At the end of `.github/workflows/ci.yml`, append:

```yaml
  e2e:
    # ADVISORY: intentionally NOT in branch-protection required checks. All 3
    # specs mock the backend via page.route, so only the built frontend runs.
    # The CI/CD Doctor tracks this job's stability and proposes graduation.
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: web
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
          cache: npm
          cache-dependency-path: web/package-lock.json
      - name: Install deps
        run: npm ci
      - name: Install Playwright browser
        run: npx playwright install chromium --with-deps
      - name: Build
        run: npm run build
      - name: E2E tests
        run: npx playwright test
      - name: Upload test results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: junit-e2e
          path: web/test-results/*.xml
          retention-days: 30
```

- [ ] **Step 5: Validate YAML + frontend gates locally**

```bash
python3 -c "import yaml; d=yaml.safe_load(open('.github/workflows/ci.yml')); assert set(d['jobs']) == {'lint-and-test','frontend','e2e'}; print('ok')"
cd web && npx tsc --noEmit && npx eslint . --ext ts,tsx --report-unused-disable-directives --max-warnings 0 && echo lint-ok
```

Expected: `ok` then `lint-ok`.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml web/playwright.config.ts
git commit -m "ci: vitest JUnit output + advisory Playwright e2e job"
```

---

### Task 4: PR + verify the new pipeline on the PR itself

**Files:** none (git/gh only)

- [ ] **Step 1: Push and open the PR**

```bash
git push -u origin feat/ci-junit-wiring
gh pr create --title "ci: JUnit emission, advisory e2e job, trigger dedupe (CI/CD Doctor plan A)" --body "$(cat <<'EOF'
Prerequisite sensing surface for the CI/CD Doctor (spec: docs/superpowers/specs/2026-07-03-cicd-doctor-design.md).

- Dedupe push/pull_request double-run (`push` now master-only) + cancel superseded PR runs
- Python suites emit JUnit XML (core unittest suite folded into pytest); pip cache
- vitest emits JUnit XML
- NEW advisory `e2e` job (Playwright, backend fully mocked) — intentionally NOT a required check; it is the Doctor's first graduation candidate
- Artifact naming contract: `junit-python` / `junit-vitest` / `junit-e2e`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Verify the PR run produces the artifacts and the e2e job passes**

```bash
gh pr checks --watch
gh api "repos/steiner385/ccswitch/actions/runs?event=pull_request&per_page=1" -q '.workflow_runs[0].id' \
  | xargs -I{} gh api "repos/steiner385/ccswitch/actions/runs/{}/artifacts" -q '.artifacts[].name'
```

Expected: `lint-and-test`, `frontend`, `e2e` all green; artifact names include `junit-python`, `junit-vitest`, `junit-e2e`. If `e2e` fails, fix within this PR (it's new code); if artifacts are missing, the upload paths are wrong — fix before merging.

- [ ] **Step 3: Ready + auto-merge**

```bash
gh pr merge --squash --auto
```

Expected: auto-merge armed; PR lands once `lint-and-test` + `frontend` are green (e2e is not required — confirm it does NOT gate the merge).
