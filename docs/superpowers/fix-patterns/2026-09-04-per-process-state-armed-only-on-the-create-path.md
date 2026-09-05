# Fix pattern: per-process state armed only on the CREATE path

**Date:** 2026-09-04
**Origin:** Task 1.8 (assistant idle reap, #1067). The reap makes "an assistant record whose
process is gone" a routine state, which exposed what the resume path does with it. One instance
was found by reading the resume code; this is the sibling sweep required by `CLAUDE.md`'s
"audit recurring fix patterns" rule.

## The bug signature

A session's *record* outlives its *process*. Anything scoped to the process — a credential, an
in-process server, a client connection, an options overlay — must therefore be re-established
every time a driver is built. But the natural place to write that code is the **create** path,
because that is where the feature was first implemented:

```python
# _spawn_session — the create path
if is_assistant_kind(kind):
    api = AssistantApi(token=mint_token(identity, session_id), ...)
    self.register_assistant_overlay(session_id, assistant_options(server=build_server(api), ...))
driver = SessionDriver(..., assistant_api=api)
```

`SessionOrchestrator` builds a `SessionDriver` at **four** sites, and only one of them is create:

| Site | When | Had the arming? |
|---|---|---|
| `_spawn_session` | first spawn | yes |
| `ensure_driver` | resume after detach / idle reap / restart | **no** |
| `_reassign` | account rotation, 401 recompose, manual switch | **no** |
| `adopt` | adopting an external `claude` process | n/a (never an assistant) |

The give-away is a *kind-dependent* decision that the rebuild paths already make for OTHER
resources. Both `ensure_driver` and `_reassign` were already kind-aware — each calls
`_isolated_dir_for(cfg, rec.kind)` and `_suggester_for(rec.kind)` — so the fix was not "teach them
about assistants", it was "they know about assistants for two of three resources".

## What it costs

Two different failure modes, both silent:

1. **`ensure_driver` (resume).** No overlay registered → `_session_factory` passes no
   `options_overlay` → the resumed driver is a **plain coding agent**: the assistant system prompt
   gone, the `ccswitch` MCP server gone, and every builtin tool (`Bash`, `Write`, `Edit`) back ON,
   attached to the user's own assistant transcript. A security posture that holds for 15 minutes
   and then quietly inverts is worse than one that never existed.
2. **`_reassign` (rotation).** The STALE overlay is still registered, so the rotated assistant
   keeps tools that are bound to the `AssistantApi` whose httpx client `old_driver.aclose()` just
   closed. It looks healthy and fails every tool call. The new driver also holds
   `assistant_api=None`, so nothing closes the next client either.

Mode 2 is the nastier shape: leaving stale per-process state registered is worse than dropping it,
because dropping it fails loudly at the next use while staleness fails at the next *call*.

## The fix

One arming function, called at every driver build — not a copy per site:

```python
def _arm_assistant(self, session_id, identity, model) -> AssistantApi:
    """Mint the ccas1: token, build the in-process MCP server around it, register the
    options overlay. Both spawn and every rebuild call this."""
```

and the symmetric release folded into the shared teardown (`_pop_driver_state`), so the overlay
dies with the driver it belongs to on all three teardown paths (close, archive, reap).

Note the "armed per build, not per call" detail inside `ensure_driver`: its `_build()` closure can
run **twice** (the fresh-start fallback when a transcript is genuinely gone), and the second driver
must not inherit the first one's closed client.

## Where else to look

Ask of any per-session resource: *is it created in `_spawn_session` and nowhere else?* Candidates
that already do this correctly and are worth keeping correct — the X11 display lease
(`start_display` is at all rebuild sites), the Copilot proxy (`_copilot.ensure` likewise), the
isolated config dir, and the suggester. Anything new that joins that list needs all four sites, or
it is this bug again.

`server/tests/test_assistant_reap.py::test_every_driver_rebuild_rearms_an_assistant` is the
regression pin for the rotation half; the resume half is pinned by
`test_reaped_assistant_resumes_with_its_overlay_and_a_fresh_token`.
