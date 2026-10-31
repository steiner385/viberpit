# CI/CD Doctor: systemic red

Design: `docs/superpowers/specs/2026-10-31-cicd-doctor-systemic-red-design.md`.

The CI/CD Doctor's systemic-red loop runs every 120 s. It watches merge_group runs, and when
one job fails on candidates of 3 or more distinct PRs within 60 minutes (or on main plus one
candidate), it treats that as one incident:

- it sends one push alert (`ci_systemic_red`) when the incident opens and one when it clears;
- it opens one issue labeled `ci-doctor` + `ci-systemic`;
- it puts a card on the CI/CD Doctor automation, with **Clear brake** and **Dismiss** actions.

## Modes

| Mode | What it does |
|---|---|
| `off` | Nothing runs. Any live brake is lifted on the next tick. |
| `propose` (default when enabled) | Alerts, incident issue, card. Never brakes, never dispatches. |
| `correct` | Adds the brake and one fix session per incident, at most 3 a day per repo. Each is gated by its own setting: `systemic_brake` and `systemic_dispatch_fix`. |

To use `correct`, choose it explicitly on the CI/CD Doctor card. An enabled row with no mode set
resolves to `propose`.

## The brake

For an `external_feed` or `main_regression` incident in `correct` mode, the Doctor adds the
label `ci:systemic-red` to the incident issue. It also writes a body marker:
`<!-- viberpit:systemic-brake {"job": …, "expires_at": …} -->`.

KinDash's `main-red-check.sh` (cairnea/KinDash#20319) reads open issues with that label and
freezes arming and force-enqueuing while the marker is unexpired. Already-queued entries are
left alone.

How long it lasts:
- **TTL:** 240 min, renewed every tick while the incident is live.
- **Hard ceiling:** 600 min. After that the brake is lifted for good and you get one push.
- **Consumer cap:** KinDash ignores any marker more than 12 h out.

How to lift it now:
- close the incident issue; or
- remove the `ci:systemic-red` label; or
- press **Clear brake** on the card; or
- switch the Doctor to `propose` or `off`.

A brake that has been lifted is never re-applied to the same incident.

`poisoner`, `infra` and `unknown` incidents never brake. A poisoner issue includes the owner's
lock-gated dequeue command; the Doctor never runs it.

## AWS spend alarms

To relay the CloudWatch alarms from cairnea/infra#78 as `ci_spend_alarm` pushes, set
`aws_alarm_prefix` to `cairnea-ci-`. Also set `aws_profile` if the daemon host's default AWS
profile is not account 854209349783. The Doctor polls
`aws cloudwatch describe-alarms --state-value ALARM` (read-only) every 5 minutes.
