# Impeccable: existing pilots and remaining evaluation

Reviewed 2026-10-07. **Impeccable has already been piloted in KinDash,
FridgeSheet, and ViberPit.** The framework standardization effort builds on that
work. `candidate` in ViberPit's new framework lock describes formal admission to
the proposed managed standard; it does not mean the tool is unused or unpiloted.

## Historical evidence

These are representative merged PRs retrieved from GitHub, including their
reported measurements and verification limits. We have not rerun their historical
browsers, recovered every local artifact, or independently reproduced every count.
They establish actual adoption and observed outcomes, not a controlled estimate
of the framework's causal effect. Search results are discovery leads, not a count
of every invocation or an exhaustive pilot census.

| Project | Adoption and execution evidence | Outcome evidence and limits |
|---|---|---|
| KinDash (`cairnea/KinDash`) | [#17253](https://github.com/cairnea/KinDash/pull/17253) installed CLI 4.1.0, skill 4.3.1 and engine 0.1.5, ran owner-assisted init, `context`, `engine-probe` and detector commands. It records per-machine opt-in hooks in ignored local settings. [#17869](https://github.com/cairnea/KinDash/pull/17869) exposed the existing Claude payload to Codex and verified discovery configuration and an engine probe. | [#17425](https://github.com/cairnea/KinDash/pull/17425) describes a finance critique/refinement round, 1,108 Storybook captures over six widths and 144 real-app captures. Reported before/after: crashing captures 102→0, overflow 33→0, console errors 104→0, undersized-target captures 41→29. It explicitly records remaining target issues and test/environment failures. [#17581](https://github.com/cairnea/KinDash/pull/17581) adds reflow/kiosk regression checks and records 24/40 versus 22/40 critique scores; only one of three target heuristics met its goal, and browser tests were not rerun after the final fixes. Codex discovery/probe does not establish every Codex workflow or resume/rotation combination. |
| FridgeSheet (`steiner385/fridgesheet`) | [#216](https://github.com/steiner385/fridgesheet/pull/216) installed the Claude project skill with `--no-hooks`; [#217](https://github.com/steiner385/fridgesheet/pull/217) records the init interview and product constraints; [#218](https://github.com/steiner385/fridgesheet/pull/218) documents the incumbent design system, screen/print parity and detector disagreements. | [#219](https://github.com/steiner385/fridgesheet/pull/219) reports a critique-driven color/type pass, 2,188 passing tests and 390/1440px browser checks. [#228](https://github.com/steiner385/fridgesheet/pull/228) records a re-critique and selected fixes: contrast regression tests, touch floors, phone reflow and Cancel focus restoration; 2,279 tests passed. Reported scores: Today 27→27, Plan 26→29, Assignments 25→28. The unchanged score and retained design exceptions matter: value is not universal improvement in a critique number. |
| ViberPit (`steiner385/viberpit`) | [#2521](https://github.com/steiner385/viberpit/pull/2521) installed distinct Claude/Codex provider builds with `--no-hooks`; [#2522](https://github.com/steiner385/viberpit/pull/2522) records init and product context. Later [#2577](https://github.com/steiner385/viberpit/pull/2577) documents the incumbent design, runs `doctor`, and records `hooks on` with local consent/manifests. The uncommitted Codex hook manifest was deliberately excluded because dispatched sessions could not approve it. | [#2536](https://github.com/steiner385/viberpit/pull/2536) adds Sessions heading/title accessibility coverage and phone header reflow. [#2552](https://github.com/steiner385/viberpit/pull/2552) reports critique-driven reduction of retained pipeline rows in the lead from 169→21, desktop board height 24,468→4,154px. [#2570](https://github.com/steiner385/viberpit/pull/2570) fixes the phone's off-screen action and adds consequence-aware confirmation. These are practical improvements reported by shipped PRs, not evidence that every finding was correct or every runtime boundary was enforced. |

## What this changes in the plan

- Count all three projects as existing practical pilots. Do not reinstall, repeat
  onboarding interviews, or require another first-use pilot merely because the new
  framework admission manifest did not exist at the time.
- Reuse existing product/design context, useful regression tests, critique records,
  owner decisions and acceptance evidence. Preserve local differences, including
  provider packaging and hook consent. This report makes no remote project changes.
- Audit the remaining enforcement and portability gaps against these deployments:
  installed identity, trusted evidence, native execution boundaries, hook policy,
  resume/rotation/remote hosts, rollback and exceptions. Historical use does not
  grant blanket approval for new tools, broader permissions or automatic upgrades.
- Keep the backend-only routing/onboarding test: FridgeSheet has a user interface
  even though its backend is Python, so it does not substitute for a backend-only
  negative applicability fixture.

## Next measurable increment

`framework-impeccable-ui-calibration` runs trusted synthetic UI fixtures in real
Chromium without an application server or native agent. It checks clean, broken
and single-defect controls for keyboard/function behavior, names, narrow-screen
layout, touch sizing and reduced motion. Its output is **acceptance calibration**:
a correctly rejected broken fixture is a pass for the checker, not a successful
UI or an Impeccable repair. The report must retain the observed and expected
finding IDs rather than collapse those meanings into one success count.

Use `python scripts/run_evals.py --kind browser --report /path/to/report.json`
with the repository's installed Playwright Chromium. Static evals remain the
ordinary Python-only gate; `--kind agent` remains listing-only. No paid/native
agent session is launched by the browser command. Missing Chromium, no cases,
timeout or an absent/malformed execution result must fail visibly.

The next paired experiment can reuse these acceptance checks against actual
baseline and candidate outputs from frozen tasks drawn from the historical
failures. It still needs an isolated native-agent execution boundary, explicit
budget/model/version recording and owner rubric for subjective outcomes. Do not
claim that fixture calibration has run that experiment, assessed visual taste,
or authenticated an admission decision. Publish whatever historical cost and
artifact data can be recovered; leave missing fields unknown, not zero.

## Reusable planning correction

Before proposing adoption of an already-installed framework, search the actual
upstream project repositories and merged PRs for installation, live commands,
follow-up fixes, verification and owner decisions. Do not infer absence of use
from absence of a new governance artifact. Keep historical adoption, repeatable
acceptance checks, controlled paired evaluation and formal standard admission as
separate evidence claims. A gap in one does not erase evidence in the others.
