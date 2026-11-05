---
slug: release-milestones
parent: product-management-layer
title: Release milestones
state: delivering
links:
  specs:
    - docs/superpowers/specs/2026-11-05-release-milestones-design.md
    - docs/superpowers/plans/2026-11-05-release-milestones-phase2.md
  code:
    - server/pm/milestone_def.py
    - server/pm/milestone_github.py
    - server/pm/milestone_status.py
    - server/pm/milestone_refresh.py
    - server/github/actions_api.py
    - server/automations/milestones_refresh.py
    - server/api/pm_milestones.py
    - web/src/api/milestones.ts
    - web/src/features/milestones/
  prs: ['#3302', '#3304', '#3316']
---

# Release milestones

A product's release ladder: ordered milestones, each with a gate of measured conditions, kept current from GitHub and the product's own CI.

**Problem** — A product's path to release (household → friends → community → public) lived in a spec
and in people's heads. Whether a milestone's gate was met meant reading several GitHub searches, epics,
workflow histories and repo-computed numbers by hand, and the answer was stale the moment it was
written. Nothing told the operator which condition was blocking, or how old a reading was.

**Target users** — The product owner deciding when a milestone is reached, and the sessions and
automations that work toward it and need to know what still blocks the gate.

**Desired outcome** — Each product declares its ladder in one file in its repo. ViberPit reads every
GitHub-backed and CI-published condition on a schedule, records each reading with the time it was
measured, and shows the ladder with each gate's met / unmet / unknown state. A stale or failed reading
is never shown as met.

**Success metrics** — (unknown — needs owner input). Candidate measures: the share of gate conditions
measured automatically rather than ticked by hand; the age of the oldest reading on the current
milestone; the time from a gate being met to the milestone being marked reached.

**Non-goals** — Not several concurrent ladders per product. Not running product code on ViberPit
hosts: a repo-computed number is published by the product's own CI as an artifact, and ViberPit only
reads it. Not a replacement for the roadmap: a milestone's leaves are its epics' sub-issues, which stay
on the roadmap. The full list is in the spec, §13.

**Notes** — State is `delivering`. Phase 1 (#3316) shipped the live read-only ladder from the repo
file; Phase 2 adds CI-published metrics and managing the ladder from ViberPit.

## The definition file

The ladder lives at `docs/product/milestones.yaml` on the default branch of the product's primary
repo, and pm-sync reads it as it reads feature docs. The full schema is in the
[spec, §5](../../superpowers/specs/2026-11-05-release-milestones-design.md). In short:

- `version: 1`, a `title`, an optional `spec` link and an optional `start` milestone key.
- `milestones`, in ladder order. Each has a `key` (`[a-z0-9-]+`), a `name`, optional `label`,
  `audience`, `platforms`, `epics` and `refs`, and a `gate` of conditions.
- An optional `numbers` panel of display-only conditions.
- A condition is `{key, label, measure}`. A measure has a `kind` (`sub_issues`, `issue_count`,
  `refs_closed`, `workflow_streak`, `ci_metric`, `ladder_leaves`, `manual`), its kind's parameters, an
  optional `op` (`eq ne lt le gt ge`) and `target`, and an optional `max_age` in seconds.
- A gate condition needs `op`/`target`, except the all-closed kinds and `manual`. A `numbers` entry
  without them is display-only and reads `info`.

The parse is strict: an unknown key or kind, a duplicate key or a bad ref is an error, and ViberPit
keeps the last good definition.

## CI metrics contract

A `ci_metric` condition reads a number the product's CI computed. ViberPit never runs product code;
the product publishes the number as a workflow artifact.

```yaml
measure: {kind: ci_metric, workflow: milestone-metrics.yml, metric: my_metric, op: eq, target: 0}
```

`workflow` is the workflow file name and `metric` the key to read. `branch` is optional and defaults
to the repo's default branch.

**The artifact.** The workflow uploads one artifact named **`viberpit-metrics`** containing
**`metrics.json`** at its root:

```json
{"version": 1, "metrics": {"my_metric": 293}, "sha": "<commit>", "generated_at": "<ISO-8601>"}
```

`version` must be `1` and `metrics` must be an object of numbers. `sha` and `generated_at` are for
people reading the artifact; ViberPit does not use them.

**Which run is read.** ViberPit lists the workflow's 10 newest runs on the branch, keeps the
completed `success` runs, and reads the newest of them that has a non-expired `viberpit-metrics`
artifact, probing at most 3. Each `(workflow, branch)` is read once per refresh, however many
conditions name it.

**Freshness.** The reading's time is that run's `updated_at`, its completion time, so the age
shows when the number was computed. A `ci_metric` reading is stale after 36 hours unless the
condition sets `max_age`. A stale reading shows as *unknown*. Run the workflow on push to the default
branch and on a daily schedule so it stays fresh.

**Errors.** Each of these leaves the condition *unknown* and keeps its last value and time:

| Error | Meaning |
|---|---|
| `no successful run of <workflow>` | None of the 10 newest runs on the branch succeeded, or the workflow has no runs there. |
| `no viberpit-metrics artifact in the last 3 successful runs` | The workflow ran but did not upload the artifact, or it expired. |
| `metric '<m>' missing` | `metrics.json` has no key `<m>`. |
| `metric '<m>' is not a number` | The value is a string, boolean, null or other non-number. |
| `metrics.json: <reason>` | The artifact is over 5 MiB, is not a readable zip, has no `metrics.json` (or one over 1 MiB), is not JSON, or has the wrong `version` or `metrics` shape. |
| `metrics artifact read failed` | GitHub refused or failed the artifact read. |
| `workflow runs read failed: <reason>` | The run list could not be read, for example because the workflow file does not exist. |

**Example workflow.**

```yaml
name: Milestone metrics
on: { push: { branches: [main] }, schedule: [{ cron: '0 6 * * *' }], workflow_dispatch: {} }
jobs:
  metrics:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: |
          n=$(./scripts/count-something.sh)
          printf '{"version":1,"metrics":{"my_metric":%s},"sha":"%s","generated_at":"%s"}\n' \
            "$n" "$GITHUB_SHA" "$(date -u +%FT%TZ)" > metrics.json
      - uses: actions/upload-artifact@v4
        with: { name: viberpit-metrics, path: metrics.json, retention-days: 14 }
```

**Evidence** — `server/pm/milestone_def.py` (strict parser and measure-kind registry);
`server/pm/milestone_github.py` (the batched GraphQL read); `server/pm/milestone_refresh.py` (the
refresh, including the `ci_metric` read); `server/github/actions_api.py` (`metrics_artifact`, the
artifact read); `server/pm/milestone_status.py` (evaluation and read-time freshness);
`server/automations/milestones_refresh.py` (the 10-minute refresh loop). Tests:
`server/tests/test_pm_milestone_refresh.py`, `server/tests/test_actions_api_metrics_artifact.py`,
`server/tests/test_pm_milestone_def.py`.
