# Deploy Monitor

Automation id `deploy-monitor` (Delivery › Automations). Watches every managed repo's GitHub
**Deployment** records — the ones Railway and GitHub environments write per push — and turns a
failing or stalled environment into one incident.

| Mode | Behaviour |
|---|---|
| `off` | nothing |
| `watch` | incident in the feed, one `deploy_failed` notification, card actions; never opens an issue or comments on one — but an issue the incident already holds (filed earlier in `file` mode, or via the card's *File issue* action) is still closed with a recovery comment on recovery, and issue state is still read |
| `file` | `watch` + one `deploy-monitor` issue per incident in the repo: comment when the failing sha moves (at most every *comment gap*), close with a comment on recovery |

**Signal.** An environment is *failing* when its newest deployment's status is `FAILURE`/`ERROR`
and older than the *grace* window; *stalled* when the newest deployment has no terminal status
after *stall after* minutes. Only `qa` / `prod` / `admin` environments (`classify_env`) are watched
unless *Watch every environment* is on. A repo whose deployments cannot be read leaves its
incidents untouched — the monitor never closes an incident on missing data. If a later, successful
read no longer shows an incident's environment (a rename, or it rolled out of the deployment
window) — or the repo itself leaves the watched set — the incident is not auto-deleted: the feed
notes it once (`vanished`) and an operator dismisses it from the card.

**Card actions.** `File issue` (no open issue, not muted, mode not off), `Mute`/`Unmute`, and
`Dismiss` on every incident — deletes it, first closing (with a comment) any issue it still holds,
in any mode. A muted incident is excluded from the card's error/warn level and called out by count
(e.g. "3 incidents (1 muted)") rather than disappearing.

**Humans closing the issue.** Closed as *not planned* mutes the incident until recovery. Closed
as *completed* acknowledges the current sha; if a later sha still fails, a new issue is filed
linking the old one.

**Notifications.** The feed's `incident` entry is informational; `deploy_failed` (via the
notification rules, fanning out like *PR broke* when no rules exist) is the actual alert — scope a
rule by repo, or ask the assistant to "watch deploy cairnea/KinDash fails".

**Queue priority.** An issue filed in `file` mode carries the `deploy-monitor` label, which the
backlog queue treats as urgent (`server/pm/queue.py:URGENT_LABELS`): it sorts ahead of ordinary
backlog in `rank_key` and its group is floated to the front of the grouping plan. Without that it
sorted *last* — a filed incident is unscored and brand new, the two worst positions under a
score-then-oldest ordering, so #16150 and #16151 sat at 328th and 329th of 332 while both admin
environments stayed down. Priority reorders only; it does not override a product's `dispatch: off`,
its `max_in_flight`, or a repo paused by weight, so filing an incident can never exceed a ceiling
an operator set.

Spec: `docs/superpowers/specs/2026-09-12-deploy-monitor-design.md`.
