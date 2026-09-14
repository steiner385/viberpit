# A module-level seam import freezes whichever fake is live at first import

Companion to the fix in `ownership_reconciliation` and `mechanical_update`; this
note records the sweep that shows where else the shape can hide.

## Symptom

A test file passes alone and fails only in CI. Under `pytest -n auto --dist loadfile`
whole files share a worker process, so an earlier file in the same shard decides the
outcome. `test_pr_consolidation.py::test_consolidate_duplicate_when_live_session_exists`
returned `spawning` where it must return `duplicate`, but only when
`test_backlog_runner_loop.py` — same shard — ran first in that process.

Standalone it passed for the wrong reason: with no stub reaching the guard, the real
resolver made a live GitHub call, raised `TargetIdentityError`, and the caller maps
that to `duplicate` too. A green run proved nothing.

## Anti-pattern

A module binds a patchable seam at import time:

```python
from server.automations.target_identity import resolve_targets   # module level
```

`monkeypatch.setattr("server.automations.target_identity.resolve_targets", fake)`
rebinds the attribute on the *defining* module. A module holding its own pointer
never sees the patch — and if it is first imported *while* a fake is installed, it
captures that fake permanently, since monkeypatch only restores the binding it made.

Patching by dotted string is what forces the import. `test_backlog_runner_loop.py`
patches `"server.automations.ownership_reconciliation.reconcile_legacy_targets"` on
the line right after stubbing the resolver, importing the guard at exactly the moment
its `I_<sha256(ref)>` fake is live.

The damage is silent, not loud. `_pr_doctor_claim` minted the wanted key set through
the correct resolver while `reconcile_legacy_targets` minted the legacy set through
the stale one. Two disjoint key spaces cannot intersect, so verified overlap with a
live owner read as "no conflict" and a second owner was admitted for a PR set that
already had one.

## Fix shape

Reach the seam through its module so every consumer shares one address:

```python
from server.automations import target_identity
await target_identity.resolve_targets(refs, kind="pr")   # resolved per call
```

Tests then stub the canonical defining module. A stub applied to a name re-exported
by the consumer passes locally and still leaves a frozen binding serving everyone else.

## Where to look

The defect needs a seam reached through *both* a frozen and a lazy import — that
inconsistency is what lets two resolvers disagree. Enumerate them mechanically:
collect every `monkeypatch.setattr("<mod>.<name>", …)` target in the suite, then use
`ast` to list production modules importing that same `<mod>.<name>` at module level
versus inside a function, and intersect.

Two seams over `server/` were live hazards and are fixed: `ownership_reconciliation`
freezing `resolve_targets`, and `mechanical_update` freezing `ownership.owned` — the
latter a latent copy of the same bomb, because `test_automation_launch_fencing.py`
stubs `"server.automations.ownership.owned"` with a raising Mock that would have
welded itself in. Neither appears in the sweep any more.

Five mixed-binding seams remain: `db_session` (26 modules), `record_audit` (17),
`get_settings` (14), `pick_helper_config_dir` (3) and `resolve_model_policy` (1).
These are patched per-importing-module or replaced wholesale via fixtures, so they
do not produce the two-resolvers-disagree failure — the count is a watch list, not a
backlog. Re-run the sweep when adding a lazily imported module.

## Verification that actually proves it

Reproduce by file order, not by the full suite:
`pytest server/tests/test_backlog_runner_loop.py server/tests/test_pr_consolidation.py`
fails deterministically before the fix and passes after.

`test_ownership_reconciliation.py` imports the guard at collection time, so once its
stubs point at the canonical seam they are ignored unless the lookup is lazy — 12 of
its tests fail without the production change and all pass with it. A fix here is only
proven by tests that fail without it.
