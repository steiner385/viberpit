# Slash-command SDK spike verdicts (2026-06-22)

Probe: `server/tests/integration/test_sdk_commands_spike.py`
Run with: `CCSWITCH_SPIKE=1 CCSWITCH_SPIKE_CONFIG_DIR=<path-to-account-cfg> <main-checkout>/.venv/bin/python -m pytest server/tests/integration/test_sdk_commands_spike.py -v -s`

## Status: PENDING — not yet run (no account credentials in this environment)

The probe file is written and skips cleanly. An operator with a live Claude Max account must run
the spike command above to populate the verdicts below.

---

## Gate C.1 — `get_server_info()["commands"]` entry shape: **PENDING**

**Assumed TRUE** (working assumption for downstream tasks):

Each entry is a dict with at minimum:
```python
{"name": str, "description": str, "argumentHint": str | None}
```

Task 2's `build_catalog` reads these fields defensively (using `.get()` with safe defaults), so it
is not blocked on this verdict. If the real run reveals different key names, update `build_catalog`
accordingly.

**To confirm:** check `COMMANDS SAMPLE:` in spike output for exact keys.

---

## Gate C.2 — Custom `.claude/commands/*.md` expands via `query()`: **PENDING**

**Assumed TRUE** (working assumption — this is standard Claude Code behavior).

The `/spikeecho` test creates a throwaway command file in `cwd/.claude/commands/spikeecho.md` and
sends `/spikeecho` via `query()`. A reply containing an `AssistantMessage` confirms expansion.

**Decision gate:** If this verdict is FALSE, STOP — the passthrough mechanism is the feature's
core assumption. The entire slash-command dispatcher design must be revisited before Tasks 2–6 can
proceed.

---

## Gate C.3 — `/compact` streams via `query()`: **PENDING**

**Assumed UNVERIFIED — kept in `passthrough` bucket but FLAGGED.**

`/compact` is currently classified `passthrough` (it is NOT in `EXCLUDED`). The actual `EXCLUDED`
set in `server/sessions/commands/catalog.py` contains 18 entries: `login`, `logout`, `config`,
`doctor`, `bug`, `init`, `ide`, `terminal-setup`, `vim`, `resume`, `exit`, `quit`, `upgrade`,
`release-notes`, `status`, `cost`, `export`, `add-dir`. The `CONTROL` set contains `model` and
`clear` (not `quit`). If the real run shows `/compact` produces no `AssistantMessage` (i.e.,
the stream is empty or only system messages), add `"compact"` to `EXCLUDED` in
`server/sessions/commands/catalog.py`.

**To confirm:** check `COMPACT STREAM:` in spike output. If the list contains `AssistantMessage`,
the command streams correctly. If empty or only `SystemMessage`/`ResultMessage`, it is a no-op
and should be excluded.

---

## Downstream task status

- **Task 2** (`build_catalog`, `classify`, `EXCLUDED`): Not blocked. Field reads are defensive;
  `EXCLUDED` list can be updated after the verdict run.
- **Task 4** (`/compact` handling): Not blocked. The `/compact` route is kept in `passthrough`
  pending verdict; it will be moved to `EXCLUDED` if the spike shows it no-ops.

---

## Operator checklist

1. Find or create a Claude Max account config dir (e.g. `~/.claude-accounts/primary`).
2. Run: `CCSWITCH_SPIKE=1 CCSWITCH_SPIKE_CONFIG_DIR=<dir> <main-checkout>/.venv/bin/python -m pytest server/tests/integration/test_sdk_commands_spike.py -v -s 2>&1 | tee /tmp/slash-cmd-spike.log`
3. Inspect `/tmp/slash-cmd-spike.log`:
   - `COMMANDS SAMPLE:` → record actual key names and update Gate C.1.
   - `test_custom_command_expands_via_query` PASS/FAIL → update Gate C.2.
   - `COMPACT STREAM:` → if empty, add `"compact"` to `EXCLUDED` in Task 2 and update Gate C.3.
4. Update this file with real verdicts and remove the PENDING markers.
