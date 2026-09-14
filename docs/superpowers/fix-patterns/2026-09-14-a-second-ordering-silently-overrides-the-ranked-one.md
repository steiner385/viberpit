# Fix pattern: a consumer re-derives an ordering and silently overrides the ranked one

## Symptom

A priority system looks correct everywhere you inspect it and is wrong at the only
point that acts on it. The ranking function has tests, the operator's queue view
renders in the right order — and the thing that should be first is worked on last.

2026-09-14: the Deploy Monitor filed `cairnea/KinDash#16150` nine minutes after two
environments went down. Three days and eight failed builds later nothing had picked it
up. The live plan read `groups=332 rationale_null=332`, `#16150` at rank **328**, with
everything ahead of it in ascending issue-number order.

## Anti-pattern

A consumer downstream of a ranked collection re-derives its own order instead of
preserving the one it was handed. `backlog_grouping.parse_group_plan` sorted unplaced
candidates by `(number, repo)` — while `candidates` had arrived sorted by
`server.pm.queue.rank_key`, the module documenting itself as "one definition of what
agents will do next".

Three things kept it invisible:

- **The two orderings agreed by accident.** No board carries priority scores, so
  `rank_key` degrades to age order, and issues are numbered in creation order — so the
  override produced an identical list. It only diverges when a row must *jump* the
  queue, the one case nobody had exercised.
- **The override lived on the "fallback" path, which is the live one.** The re-sort
  applies to candidates the model did not place; in production the model places nothing
  (`rationale_null=332`), so the fallback is the whole behaviour. A fallback that always
  runs is not a fallback.
- **The first fix treated the symptom.** #1698 floated urgent groups to the front of the
  plan, which correctly fixes the incident case and leaves the re-sort in place beneath
  it — so every *non*-urgent row still lost its ranked position, and a human-scored
  issue was still reordered by issue number. Floating one class over a wrong ordering is
  not the same as fixing the ordering.

A related shape at one remove, found in the same investigation: `QueueEntry` carried
`score` and `gh_created_at` for `rank_key` but not urgency. **Any field the sort key
reads must exist on every type the key is applied to**, or the narrower type silently
sorts as if the leg were absent — and the narrower type is usually the one on the hot
path (here the runner's per-product rank → merge → re-sort). Fixed in #1698.

## Fix shape

- The consumer preserves the order it received: iterate the ranked collection rather
  than `sorted(collection, key=...)`. A ranked collection is an *answer*, not an
  unordered bag; re-sorting discards the computation upstream just performed.
- If determinism was the reason for the sort, check whether the upstream order is
  already total. `rank_key` ends in `ref`, so it was — the sort bought nothing.
- Put a new priority rule in the ranking core, not the consumer: one definition, and
  the operator's view and the runner's behaviour cannot disagree.
- Keep the symptom-level guard where it covers a *different* path. #1698's float still
  earns its place: it guards the model-ordered path, where a model free to order by its
  own idea of "implementation priority" can rank an incident last. Preserving candidate
  order does not help there.

## Where to look

- Any `sorted(...)`/`.sort(...)` whose input is already ranked. Grep consumers of
  `to_candidate`, `_backlog_last_queue`, or anything named `*_ranked`/`*_queue` and ask
  what order it arrived in. Legitimate exception: `backlog_selection.order_live_groups`
  re-orders for per-repo fairness but is documented *stable*, so plan priority survives
  within a repo — a stable reorder on a different axis is fine; re-sorting on a competing
  definition of the *same* axis is not.
- Any sort key applied to more than one type (`rank_key` takes `QueueRow` and
  `QueueEntry`): every leg must exist on both.
- A "fallback" ordering — measure how often it actually runs before trusting it is rare.
  `GET /api/automations/backlog-runner/groups` with every `rationale` null means the
  model placed nothing and the fallback is the whole system.

## Reference

Issue #1697. #1698 added urgency to `rank_key` and floated urgent groups in the plan;
this pattern's own fix (#1699) removed the underlying re-sort. The KinDash break that
surfaced it is cairnea/KinDash#16150 / #16151, fixed in cairnea/KinDash#16192.
Test: `test_leftover_order_preserves_score_and_age_not_just_urgency`.
