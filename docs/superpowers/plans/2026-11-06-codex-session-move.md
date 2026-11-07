# Move a Codex session between fleet hosts (D-17 follow-up, #3347) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox syntax.

**Goal:** the D-17 move dialog accepts an idle **Codex** session. The saga (`server/sessions/session_move.py`)
stays as built for Claude; the Codex engine adds one step on each daemon half: the source exports the
thread's rollout file (plus ViberPit's per-thread sidecars) after its driver closes, the target writes it into
its own `CODEX_HOME` before resuming by thread id, and the source retires its copy at finalize.

**Facts (binding):** `docs/superpowers/specs/2026-11-06-codex-session-move-verification.md` (codex-cli 0.153.2):
a thread is ONE file `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<threadId>.jsonl`; copying it into the same
relative path under another `CODEX_HOME` and calling `thread/resume {threadId, cwd}` works with no replay;
`thread/resume` takes no path. Real rollouts on dobby today: 357 files, the largest 52 MB
(`/var/lib/viberpit/state/ccswitch/codex/<account>/sessions/…`).

**Rulings taken here (safe, reduce scope):**
- **Transport: inline in the manifest**, base64, through the existing mTLS RPC, capped at `HISTORY_MAX_BYTES = 128 MiB`
  of raw bytes (rollout + sidecars). Over the cap is a predicate-level refusal `MOVE_HISTORY_TOO_LARGE` (nothing
  touched). The edge strips the bytes before `_store_manifest` — `move_manifest` keeps only the descriptor
  (relPath, size, sha256), which is what a reconcile verifies a holder against.
- **Never overwrite:** a rollout for that thread id already under the target's `CODEX_HOME` refuses the import
  (`MOVE_HISTORY_EXISTS`, restore to the source).
- **Finalize retires, never deletes:** the source renames its rollout to `<name>.jsonl.moved-to-<owner>` so nothing
  on the source can resume it while the bytes stay recoverable (the analogue of the recovery ref).
- **Account identity:** accounts are fleet-wide (the shared account store), so the target's `ensure_driver` resumes
  under the same `account` and its own `CODEX_HOME` for it (`manager.resolve_home(account)` — created if absent).
- **Sidecars** (`<account cfg dir>/codex-item-times/<thread>.jsonl` and `<thread>.turns.jsonl`) travel in the same
  bundle and are written on the target only when absent (on the deployed fleet that directory is the shared
  store, so they are usually already there).

## Global constraints

- Constitution III: file layout and resume behaviour come from the spike; nothing else is assumed. Thread ids are
  validated with `item_times._SAFE_ID` (make it the public `safe_thread_id()`), relPaths are validated against
  `sessions/\d{4}/\d{2}/\d{2}/rollout-[^/]*-<threadId>\.jsonl` before any write. Never follow symlinks
  (`O_NOFOLLOW`), write via temp file + fsync + rename, mode 0o600.
- No new WARNING sites inside the saga steps (each WARNING auto-files an issue): refusals and expected failures log
  INFO and ride the `MoveError`.
- `move_manifest` must stay small: `sm.storable_manifest(manifest)` drops every `data` field.
- Codex refusal copy changes: `MOVE_ENGINE_UNSUPPORTED` becomes generic (no engine is named).
- Gates: ruff, mypy, focused pytest, then the full `server/tests -n 4` in a clean env (`env -i` + isolated HOME,
  see memory); web typecheck/lint/full vitest/build/budget.

### Task 1: `server/codex/thread_move.py` (pure file logic) + `server/tests/test_codex_thread_move.py`

API (exact names; Task 2 codes against them):

```python
class ThreadMoveError(Exception): code: str; detail: str   # codes: "not_found", "ambiguous", "too_large", "exists", "bad_bundle", "io"
HISTORY_MAX_BYTES = 128 * 1024 * 1024
@dataclass(frozen=True) class ThreadBundle:
    thread_id: str; rel_path: str; size: int; sha256: str
    data: str | None            # base64 of the rollout; None once stripped
    sidecars: tuple[Sidecar, ...]   # Sidecar(name: str, size: int, sha256: str, data: str | None)
    def to_dict(self) -> dict; @classmethod from_dict(cls, d: dict) -> "ThreadBundle"; def stripped(self) -> "ThreadBundle"
def find_rollout(home: Path, thread_id: str) -> Path | None     # glob sessions/*/*/*/rollout-*-<id>.jsonl; regular file only; >1 → ThreadMoveError("ambiguous")
def inspect(home: Path, sidecar_root: Path | None, thread_id: str) -> int   # total bytes the export would carry; 0 when no rollout
def export_thread(home, sidecar_root, thread_id, *, max_bytes=HISTORY_MAX_BYTES) -> ThreadBundle
def import_thread(home, sidecar_root, bundle) -> list[Path]     # paths it WROTE (rollout first); refuses an existing rollout ("exists"); sidecars only when absent
def verify_thread(home, bundle) -> bool                          # rollout at rel_path has bundle.size and bundle.sha256 (read to EOF)
def remove_imported(paths: list[Path]) -> None                   # abandon: unlink exactly what import_thread wrote; never raises
def retire_rollout(home, thread_id, owner: str) -> Path | None   # rename to "<name>.moved-to-<owner>"; None when no rollout
```

Tests: round trip between two tmp homes; sidecars carried when absent, untouched when present; unsafe thread id →
not_found/None (never a glob with metacharacters); traversal rel_path refused on import; existing rollout refused;
over-cap refused at inspect/export; verify false on a truncated copy; retire renames and a second find is None;
remove_imported removes only what was written; from_dict rejects a bad shape.

- [ ] failing tests first, then the module, ruff + mypy clean.
- [ ] commit `feat(codex): thread rollout export/import for the fleet move (#3347)`.

### Task 2: server integration

Files: `server/codex/capabilities.py` (`cross_host_transfer=True`, rationale cites the spike), `server/sessions/engine.py`
(docstring), `server/codex/manager.py` (`resolve_home(account) -> Path` public wrapper over `_resolve_home`),
`server/sessions/session_move.py`, `server/sessions/orchestrator.py`, `server/tests/test_session_move.py`.

session_move.py:
- `MOVE_HISTORY_TOO_LARGE`, `MOVE_HISTORY_EXISTS` reason codes; `HISTORY_MAX_BYTES` re-exported from thread_move.
- `WorktreeFacts.history_bytes: int = 0` (`from_preflight` reads `historyBytes`); `movable()` refuses
  `MOVE_HISTORY_TOO_LARGE` when `facts.history_bytes > HISTORY_MAX_BYTES`.
- `MOVE_ENGINE_UNSUPPORTED` copy: "This session's engine cannot move between hosts." (drop the Codex branch).
- `storable_manifest(manifest) -> dict` (drops `codexThread` data via `ThreadBundle.stripped()`); the saga calls it
  before `_store_manifest`.

orchestrator.py:
- `_codex_thread_paths(account) -> tuple[Path, Path]`: `(self._codex.resolve_home(account), self._codex_item_times_root(account))`.
- `transfer_preflight`: for a Codex record with an `engine_session_id`, `historyBytes = thread_move.inspect(...)`.
- `_transfer_export`: Codex + over cap → `MOVE_HISTORY_TOO_LARGE` BEFORE anything is touched. After
  `_close_for_transfer`: `export_thread` (errors → `MOVE_EXPORT_FAILED`, restore) and manifest gains
  `"codexThread": bundle.to_dict()`. Claude path unchanged.
- `_transfer_import`: after the worktree step and before `_install_row`, a Codex non-local import writes the
  bundle (`exists` → `MOVE_HISTORY_EXISTS`, other → `MOVE_IMPORT_FAILED`); `_abandon` runs `remove_imported` on what
  it wrote. `_verify_moved_transcript` becomes `_verify_moved_history(session_id, manifest)`: Claude = the
  transcript marker as today; Codex = `verify_thread` against the (possibly stripped) bundle, raising
  `MOVE_TRANSCRIPT_STALE` ("The Codex thread did not arrive intact.") — also on `local=True` when a bundle is in the
  manifest (the reconcile verifying a holder). The moved-notice preamble for Codex ends "…; the worktree,
  uncommitted changes and Codex thread came with it."
- `_transfer_finalize`: Codex → `retire_rollout(home, thread_id, owner)` after the close; result gains
  `"historyRetired": str | None`.
- `_engine_supports(rec.engine, "cross_host_transfer")` keeps gating; it now passes for Codex.

Tests (extend `test_session_move.py`, same fixtures; a bare orchestrator gets `o._codex = SimpleNamespace(resolve_home=lambda a: home)`):
- predicate: a Codex subject is movable; `history_bytes` over the cap refuses; the generic copy.
- `test_claude_declares_cross_host_transfer_and_codex_does_not` → both declare; wire false under the kill switch for both.
- export: Codex record carries `codexThread` (size/sha256 match the file, sidecar present) and refuses over-cap before closing; a missing rollout → `MOVE_EXPORT_FAILED` and the local restore recovers.
- import: writes the rollout at the same relPath, verifies, resumes; an existing rollout → `MOVE_HISTORY_EXISTS` and nothing written; a bad hash → `MOVE_TRANSCRIPT_STALE` and the written file removed; resume failure removes it.
- finalize: retires the source rollout (`find_rollout` → None afterwards, the `.moved-to-rocky` file holds the bytes).
- saga: `move_manifest` row holds the stripped bundle (no `data`).
- preflight reports `historyBytes`.

- [ ] failing tests, implementation, ruff + mypy + `pytest server/tests/test_session_move.py server/tests/test_codex_capabilities.py server/tests/test_agent_driver_contract.py server/tests/test_codex_thread_move.py`.
- [ ] commit `feat(fleet): move an idle Codex session to another host (#3347)`.

### Task 3: web copy

- `web/src/api/sessionMove.ts`: delete `CODEX_STAYS_COPY`. `SessionHeader.tsx`: drop `moveHostDisabledReason`
  (and the prop + inert-item branch in `SessionActions.tsx`, now dead) — the menu item shows for any engine with
  `crossHostTransfer` and hides otherwise. `moveReasons.ts`: `MOVE_ENGINE_UNSUPPORTED` → "This session's agent
  cannot move between hosts."; add `MOVE_HISTORY_TOO_LARGE` ("This session's Codex history is too large to move
  (over 128 MB).") and `MOVE_HISTORY_EXISTS` ("The target host already has a copy of this Codex thread; clear it
  first.").
- Tests: `SessionHeader.test.tsx` ("Codex: not movable" → Codex with `crossHostTransfer: true` offers it; false hides it),
  `SessionActions.test.tsx` (drop the inert-item test), `MoveSessionDialog.test.tsx` (new copy for
  `MOVE_ENGINE_UNSUPPORTED`, add `MOVE_HISTORY_TOO_LARGE`).
- [ ] full web gates: `npm run typecheck && npm run lint && npm run test && npm run build && npm run budget` (in `web/`, via `(cd web && …)`).
- [ ] commit `feat(web): Codex sessions can move to another host (#3347)`.

### Task 4: docs

- `docs/product/features/multi-host-fleet.md`: Codex moves (how: rollout + sidecars inline, cap, never-overwrite, retire at finalize); drop "no Codex".
- `docs/audits/2026-11-01-claude-desktop-code-benchmark.md`: row 178 and D22c note — Codex moves too (built, #3347); no score change.
- `docs/superpowers/specs/2026-11-06-codex-session-move-verification.md`: a "Built (2026-11-06)" section listing what landed and which gaps stay open.
- `server/codex/item_times.py` docstring: a transferred session now brings its sidecar.
- [ ] commit `docs: Codex session move (#3347)`.
