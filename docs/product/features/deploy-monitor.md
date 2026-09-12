# Deploy Monitor

Automation id `deploy-monitor` (Delivery › Automations). Watches every managed repo's GitHub
**Deployment** records — the ones Railway and GitHub environments write per push — and turns a
failing or stalled environment into one incident.

| Mode | Behaviour |
|---|---|
| `off` | nothing |
| `watch` | incident in the feed, one `deploy_failed` notification, card actions; no GitHub writes |
| `file` | `watch` + one `deploy-monitor` issue per incident in the repo: comment when the failing sha moves (at most every *comment gap*), close with a comment on recovery |

**Signal.** An environment is *failing* when its newest deployment's status is `FAILURE`/`ERROR`
and older than the *grace* window; *stalled* when the newest deployment has no terminal status
after *stall after* minutes. Only `qa` / `prod` / `admin` environments (`classify_env`) are watched
unless *Watch every environment* is on. A repo whose deployments cannot be read leaves its
incidents untouched — the monitor never closes an incident on missing data.

**Humans closing the issue.** Closed as *not planned* mutes the incident until recovery. Closed
as *completed* acknowledges the current sha; if a later sha still fails, a new issue is filed
linking the old one.

**Notifications.** `deploy_failed` fans out like *PR broke* when no rules exist; scope a rule by
repo, or ask the assistant to "watch deploy cairnea/KinDash fails".

Spec: `docs/superpowers/specs/2026-09-12-deploy-monitor-design.md`.
