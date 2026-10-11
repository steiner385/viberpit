# Always-allow standing rules — PR 1 (rule store + enforcement) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Durable, scoped, human-only "always allow" rules stored in permission profiles. The permission registry enforces them, and an edge API creates, previews, lists, edits and revokes them.

**Architecture:**
- A new pure package `server/permission_rules/` holds the pattern grammar (`patterns.py`) and the safety assessment (`safety.py`).
- A DB layer (`store.py`) keeps rules as `PermissionProfile.patterns` of *scoped auto-profiles*, plus a `permission_rule_stats` metadata table.
- A `StandingRuleGate` (`gate.py`) is injected into `PermissionRegistry`. The registry consults it only where a card would otherwise be created.
- The 1-hour `_grants` cache is deleted.
- The edge (`session_actions.py` + `server/api/permission_rules.py`) owns creation, the human-only guard, and resolving matching pending cards.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async (SQLite tests / Postgres prod), Alembic (reviewable record only), pytest.

**Spec:** `docs/superpowers/specs/2026-10-11-always-allow-permission-rules-design.md` (read it first; section refs below are §N of the spec).

## Global Constraints

- Run tests with the worktree runner, never a bare `pytest`: `/tmp/vp-test.sh <paths> -q`. It runs `env -i`, an isolated HOME, `VIBERPIT_DEV=1`, `PYTHONPATH=$PWD/src` and the shared venv. Create it if it is missing:
  ```bash
  cat > /tmp/vp-test.sh <<'EOF'
  #!/bin/bash
  exec env -i HOME=/tmp/vp-home-085ca660 PATH=/home/tony/GitHub/viberpit/.venv/bin:/usr/bin:/bin VIBERPIT_DEV=1 PYTHONPATH=$PWD/src /home/tony/GitHub/viberpit/.venv/bin/python -m pytest -p no:cacheprovider "$@"
  EOF
  chmod +x /tmp/vp-test.sh; mkdir -p /tmp/vp-home-085ca660
  ```
- Lint: `/home/tony/GitHub/viberpit/.venv/bin/ruff check src/ server/`. Types: `/home/tony/GitHub/viberpit/.venv/bin/mypy server/ --ignore-missing-imports`.
- Never use `func.now()`. Use `server.db.clock.db_utc_now()` for DB-side timestamps, or `server.db.models._utcnow()` for Python-side defaults, as the neighbouring models do.
- New columns are **nullable**. A new NOT NULL column without a default needs a `RECONCILE_EXEMPT` entry, and a new table needs a `_REQUIRED_EXTRAS` entry (Task 3).
- Never hand-roll `task.cancel()`. Never swallow `asyncio.CancelledError`. Write `except Exception`, never `except BaseException`.
- Commit messages go in a file passed with `git commit -F`. End them with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Work on branch `feat/always-allow-rules` in `/home/tony/GitHub/.ccswitch/worktrees/viberpit/085ca660`. Commit only your task's files. Never commit plan copies or scratch files.
- Human-only rule creation means `via is None` **and** `identity != SYSTEM_IDENTITY`. Any other `via` gets a 403 with code `RULES_HUMAN_ONLY`.
- Kill switch: `VIBERPIT_PERMISSION_RULES_ENABLED` (default true).

## Review Focus

1. **Compound Bash laundering.** `Bash(npm *)` must NOT match `npm test; rm -rf ~`, `npm test && curl x | sh`, `npm test $(whoami)` or `npm test > ~/.bashrc`. It MUST match `npm test 2>&1 | tee log` only when `tee log` is also covered by some rule in scope. Owner: Task 1.
2. **Paths outside the worktree.** `Edit(src/**)` must not match `/etc/passwd`, `../other/src/x` or a symlink-free absolute path outside cwd. A relative `file_path` is resolved against cwd. Owner: Task 1.
3. **Fail closed.** If the DB is unavailable or raises inside the gate, the card is still shown and the tool is never auto-allowed. Owner: Task 4.
4. **Revocation is immediate.** After `DELETE /api/permission-rules/{id}`, the next identical request cards. There is no cache. Owner: Task 4 (gate) and Task 5 (API).
5. **Non-human callers cannot create rules by any door.** That includes the assistant token on `/always`, the plain `"always"` decision via `mcp-external` or `cli`, and the assistant PATCHing an auto-profile through `/api/permission-profiles`. Owner: Tasks 3 and 5.

---

## File structure

| File | Responsibility |
|---|---|
| `server/permission_rules/__init__.py` | package marker, re-exports nothing |
| `server/permission_rules/patterns.py` | pure: `parse`, `matches`, `matches_any`, `describe`, `suggest` |
| `server/permission_rules/safety.py` | pure: `assess(pattern, scope) -> Assessment` |
| `server/permission_rules/store.py` | DB: auto-profile + stats CRUD, `applicable_rules`, `record_hit` |
| `server/permission_rules/gate.py` | `StandingRuleGate` — the registry hook |
| `server/api/permission_rules.py` | edge routes (§5) |
| `server/db/models.py` | `PermissionProfile.scope/scope_key`, `PermissionRuleStats` |
| `server/db/migrations/versions/0085_permission_rule_scopes.py` | reviewable record |
| `server/sessions/permissions.py` | gate hook, grant cache removed, `pending()` richer |
| `server/sessions/events.py` | `PermissionRequest.rule_suggestions` |
| `server/sessions/claude_driver.py` | pass suggestions into the event |
| `server/bridge/ws.py` | reattach re-emit carries `always_allowed` + suggestions |
| `server/sessions/orchestrator.py` | build + inject the gate |
| `server/sessions/session_actions.py` | human-only guard, legacy-"always" mapping, `grant_always` |
| `server/api/permission_profiles.py` | hide/refuse scoped profiles |
| `server/config.py`, `server/api/settings.py` | kill switch |
| `docs/product/features/session-permissions.md` | feature doc |

---

### Task 1: Pattern grammar (pure)

**Files:**
- Create: `server/permission_rules/__init__.py` (docstring only), `server/permission_rules/patterns.py`
- Test: `server/tests/test_permission_rule_patterns.py`

**Interfaces:**
- Produces:
  - `class PatternError(ValueError)`
  - `@dataclass(frozen=True) class Pattern: tool: str; spec: str | None; kind: str`, where `kind` is one of `"bare" | "exact" | "prefix" | "glob" | "path" | "domain" | "mcp"`.
  - `parse(pattern: str) -> Pattern`. Raises `PatternError`.
  - `matches(p: Pattern, tool: str, input: dict, cwd: str | None) -> bool`. This is the single-rule check. For Bash it applies only to a single segment; use `matches_any` for whole commands.
  - `matches_any(patterns: Sequence[Pattern], tool: str, input: dict, cwd: str | None) -> Pattern | None`. Returns the first pattern (in sequence order) that participates in covering the request. For Bash, every segment must be covered by some pattern in the sequence, and the return value is the pattern that covered the first segment.
  - `describe(pattern: str) -> str`.
  - `suggest(tool: str, input: dict, cwd: str | None) -> list[str]`, narrowest first. It is unfiltered; Task 2's `offered_suggestions` filters it.
  - `FILE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})`.

Semantics (spec §1):
- **Parsing.** `Tool` with no parentheses is `bare`, unless it starts with `mcp__`, in which case it is `mcp`. `Tool(spec)`:
  - For `Bash`: a spec ending in `:*` is `prefix` (the spec stored without `:*`). A spec containing `*` is `glob`. Anything else is `exact`.
  - For `Edit`/`Write`/`MultiEdit`/`NotebookEdit`/`Read`: `path`.
  - For `WebFetch`: the spec must start with `domain:`, and the kind is `domain`.
  - Any other tool with parentheses raises `PatternError`.
  - Empty spec, unbalanced parens, or a tool name that does not match `^[A-Za-z][A-Za-z0-9_]*$` raises `PatternError`.
  - The exact/prefix/glob spec is whitespace-normalised (`" ".join(spec.split())`).
- **Bash matching.** Normalise the command the same way.
  - Reject outright (no rule matches) if the command contains `$(`, a backtick, or `<(`.
  - Strip the safe redirections `2>&1`, `>/dev/null`, `2>/dev/null` and `&>/dev/null`. If any `>` remains, reject.
  - Split on the regex `\s*(?:&&|\|\||;|\||\n)\s*`. Drop empty segments.
  - Every segment must be matched by some pattern:
    - `exact`: `seg == spec`.
    - `prefix`: `seg == spec or seg.startswith(spec + " ")`.
    - `glob`: `fnmatch.fnmatchcase(seg, spec)`, after translating so that `*` matches anything including `/`. `fnmatchcase` already does that; escape `[`, `]` and `?` in the spec first so they are literal.
    - `bare` `Bash` matches anything. It is refused at assess time, but the matcher stays honest.
- **Path matching.**
  - `file_path = input.get("file_path") or input.get("notebook_path") or input.get("path")`. If the path is relative, join it to `cwd`.
  - Normalise with `os.path.normpath(os.path.abspath(...))` and compute `rel = os.path.relpath(path, cwd)`.
  - No match if `cwd` is None, if `rel` starts with `..`, or if `rel` is absolute.
  - Match `rel` (with POSIX separators) against the spec using the gitignore-ish glob:
    - `**/` matches zero or more directories;
    - a trailing `/**` matches everything below;
    - `*` matches `[^/]*`;
    - `?` matches `[^/]`.
  - Implement this as a regex translator `_path_glob_to_regex(spec) -> re.Pattern`.
  - An `Edit(...)` pattern matches any tool in `FILE_TOOLS`. `Write(...)`, `MultiEdit(...)` and `NotebookEdit(...)` patterns match only their own tool. `Read(...)` matches only `Read`.
  - A bare `Edit` matches every `FILE_TOOLS` tool *with a path inside cwd*.
- **Domain.** Parse `urllib.parse.urlsplit(input.get("url",""))`. Match when the lowercased `hostname` equals the domain or ends with `"." + domain`.
- **MCP.** A tool name `mcp__server__tool` is matched by the exact name, or by the server prefix: the pattern `mcp__server` matches when `tool.startswith("mcp__server__")`.
- **Bare non-MCP.** `tool == p.tool`. One exception: a bare `Edit` covers all `FILE_TOOLS` (inside cwd).
- **`describe` examples:**
  - `Bash(npm test:*)` gives `` Any Bash command that is `npm test` or starts with `npm test ` ``.
  - `Bash(npm *)` gives `` Any Bash command matching `npm *` (`*` = anything) ``.
  - `Bash(npm test)` gives `` Exactly the Bash command `npm test` ``.
  - `Edit(src/**)` gives `File edits/writes under src/ in the session's worktree`. For a path spec without a trailing `/**`, use `File edits/writes matching src/*.ts in the session's worktree`.
  - `Read(x)` uses "File reads …".
  - `WebFetch(domain:example.com)` gives `Web fetches from example.com and its subdomains`.
  - `mcp__s__t` gives `The MCP tool mcp__s__t`. `mcp__s` gives `Every tool on the MCP server s`.
  - A bare tool `T` gives `Every T call`.
- **`suggest`:**
  - **Bash.** Take the first segment's normalised command; let `words = cmd.split()`. Return:
    - `Bash(<full first segment>)`. If the command has more than one segment, use `Bash(<full command>)` here.
    - If `len(words) >= 2`: `Bash(<words[0]> <words[1]>:*)`.
    - `Bash(<words[0]>:*)`.
    - Deduplicate the list.
  - **File tools.** `rel` as above. If the path is outside cwd, return `[]`. Otherwise return:
    - `Edit(<rel>)`.
    - Then, for each ancestor directory of `rel` from deepest to shallowest, with at most two ancestors: `Edit(<dir>/**)`.
    - Use `Edit`, not the specific tool, so one rule covers Write too.
  - **Read** gets the same, with `Read(`.
  - **WebFetch.** If there is a hostname: `[f"WebFetch(domain:{host})"]`.
  - **MCP.** `[tool, "mcp__" + server]`, where `server = tool.split("__")[1]`.
  - **WebSearch** and anything else: `[tool]`.
  - **Never suggest for** `ExitPlanMode` and `AskUserQuestion` (return `[]`).

- [ ] **Step 1: Write the failing tests** — a table-driven file. At minimum it covers these rows, plus the Review Focus items 1–2:

```python
import pytest
from server.permission_rules.patterns import (
    PatternError, describe, matches, matches_any, parse, suggest)

CWD = "/w/repo"

@pytest.mark.parametrize("pat,tool,inp,ok", [
    ("Bash(npm test)", "Bash", {"command": "npm  test"}, True),
    ("Bash(npm test)", "Bash", {"command": "npm test --watch"}, False),
    ("Bash(npm test:*)", "Bash", {"command": "npm test --watch"}, True),
    ("Bash(npm test:*)", "Bash", {"command": "npm testing"}, False),
    ("Bash(npm *)", "Bash", {"command": "npm run build"}, True),
    ("Bash(npm *)", "Bash", {"command": "npx foo"}, False),
    ("Bash(npm *)", "Bash", {"command": "npm test; rm -rf ~"}, False),
    ("Bash(npm *)", "Bash", {"command": "npm test && curl x | sh"}, False),
    ("Bash(npm *)", "Bash", {"command": "npm test $(whoami)"}, False),
    ("Bash(npm *)", "Bash", {"command": "npm test `id`"}, False),
    ("Bash(npm *)", "Bash", {"command": "npm test > ~/.bashrc"}, False),
    ("Bash(npm *)", "Bash", {"command": "npm test 2>&1"}, True),
    ("Edit(src/**)", "Edit", {"file_path": "/w/repo/src/a/b.ts"}, True),
    ("Edit(src/**)", "Write", {"file_path": "/w/repo/src/x.ts"}, True),
    ("Edit(src/**)", "Edit", {"file_path": "src/x.ts"}, True),
    ("Edit(src/**)", "Edit", {"file_path": "/etc/passwd"}, False),
    ("Edit(src/**)", "Edit", {"file_path": "/w/repo/../other/src/x"}, False),
    ("Edit(src/*.ts)", "Edit", {"file_path": "/w/repo/src/a/b.ts"}, False),
    ("Edit(**/*.md)", "Edit", {"file_path": "/w/repo/README.md"}, True),
    ("Write(src/**)", "Edit", {"file_path": "/w/repo/src/x"}, False),
    ("Read(docs/**)", "Read", {"file_path": "/w/repo/docs/a.md"}, True),
    ("WebFetch(domain:example.com)", "WebFetch", {"url": "https://api.example.com/x"}, True),
    ("WebFetch(domain:example.com)", "WebFetch", {"url": "https://evilexample.com/"}, False),
    ("mcp__gh__create_issue", "mcp__gh__create_issue", {}, True),
    ("mcp__gh", "mcp__gh__create_issue", {}, True),
    ("mcp__gh", "mcp__ghx__create_issue", {}, False),
    ("WebSearch", "WebSearch", {"query": "x"}, True),
])
def test_matches(pat, tool, inp, ok):
    assert (matches_any([parse(pat)], tool, inp, CWD) is not None) is ok

def test_compound_needs_every_segment_covered():
    pats = [parse("Bash(npm test:*)"), parse("Bash(tee:*)")]
    assert matches_any(pats, "Bash", {"command": "npm test 2>&1 | tee log"}, CWD) is not None
    assert matches_any(pats[:1], "Bash", {"command": "npm test | tee log"}, CWD) is None

def test_path_rule_without_cwd_never_matches():
    assert matches_any([parse("Edit(src/**)")], "Edit", {"file_path": "/w/repo/src/x"}, None) is None

@pytest.mark.parametrize("bad", ["", "Bash(", "Bash()", "Foo(bar)", "WebFetch(example.com)", "1Bash"])
def test_parse_rejects(bad):
    with pytest.raises(PatternError):
        parse(bad)

def test_suggest_bash():
    assert suggest("Bash", {"command": "npm test --watch"}, CWD) == [
        "Bash(npm test --watch)", "Bash(npm test:*)", "Bash(npm:*)"]

def test_suggest_edit():
    assert suggest("Write", {"file_path": "/w/repo/src/a/b.ts"}, CWD) == [
        "Edit(src/a/b.ts)", "Edit(src/a/**)", "Edit(src/**)"]

def test_suggest_outside_cwd_is_empty():
    assert suggest("Edit", {"file_path": "/etc/x"}, CWD) == []

def test_suggest_mcp_and_web():
    assert suggest("mcp__gh__create_issue", {}, CWD) == ["mcp__gh__create_issue", "mcp__gh"]
    assert suggest("WebFetch", {"url": "https://docs.python.org/3/"}, CWD) == ["WebFetch(domain:docs.python.org)"]
    assert suggest("ExitPlanMode", {}, CWD) == []

def test_describe():
    assert describe("Bash(npm test:*)") == "Any Bash command that is `npm test` or starts with `npm test `"
    assert describe("Edit(src/**)") == "File edits/writes under src/ in the session's worktree"
    assert describe("mcp__s") == "Every tool on the MCP server s"
```

- [ ] **Step 2:** Run `/tmp/vp-test.sh server/tests/test_permission_rule_patterns.py -q`. Expect it to FAIL (import error).
- [ ] **Step 3:** Implement `patterns.py` to the semantics above, with a module docstring pointing at spec §1. Keep it pure: no I/O, no `server.db` imports.
- [ ] **Step 4:** Re-run until everything is green. Then run `ruff check server/permission_rules server/tests/test_permission_rule_patterns.py`.
- [ ] **Step 5:** Commit `feat(permission-rules): pattern grammar, matcher and suggestions`.

---

### Task 2: Safety assessment (pure)

**Files:**
- Create: `server/permission_rules/safety.py`
- Test: `server/tests/test_permission_rule_safety.py`

**Interfaces:**
- Consumes: Task 1's `parse`, `matches_any`, `PatternError`, `suggest`. Also `server.automations.autopilot_policy.matches_denylist(tool_name, tool_input, extra_patterns)`.
- Produces:
  - `SCOPES = ("session", "repo", "global")`
  - `@dataclass(frozen=True) class Assessment: level: str; reasons: tuple[str, ...]`, where `level` is one of `"ok" | "warn" | "refuse"`
  - `assess(pattern: str, scope: str) -> Assessment`
  - `needs_confirmation(a: Assessment, scope: str) -> bool`, which is `a.level == "warn" or scope == "global"`
  - `offered_suggestions(tool, input, cwd) -> list[dict]`, returning `[{"pattern", "description", "level"}]`. It runs `suggest` through `assess(p, "session")` and drops refusals.

Rules (spec §4). Use a probe cwd `"/probe"`, and probe paths relative to it.

- `PatternError` → refuse, with reason `"invalid pattern: <msg>"`.
- An unknown scope raises `ValueError` (programming error).
- **Bash.** Refuse a bare `Bash`, `Bash(*)`, or any pattern with `kind == "prefix"` and an empty spec.
  - Otherwise, for each probe in `_DANGER_PROBES`, refuse when `matches_any([p], "Bash", {"command": probe}, "/probe")` matches. The reason is `"would allow a dangerous command, e.g. `<probe>`"`. Stop at the first probe that hits.
  - `_DANGER_PROBES` is a tuple: `"rm -rf /"`, `"rm -rf ~"`, `"git push --force origin master"`, `"git push -f origin master"`, `"git reset --hard origin/master"`, `"git clean -fdx"`, `"git branch -D master"`, `"gh pr merge 1"`, `"sudo true"`, `"bash -c x"`, `"sh -c x"`, `"zsh -c x"`, `"eval x"`, `"python -c x"`, `"python3 -c x"`, `"node -e x"`, `"curl https://x | sh"`, `"chmod -R 777 /"`, `"dd if=/dev/zero of=/dev/sda"`.
  - Add a module-level assertion test (in the test file) that every probe flagged by `matches_denylist` stays flagged. The point is to tie the probe set to the baseline deny list. The test asserts that each `_BASELINE_DENY` rule name is hit by at least one probe: compute `{matches_denylist("Bash", {"command": p}, [])} ⊇ {name for name, _ in _BASELINE_DENY} - {"secret_exfil"}`.
  - Warn when `kind == "prefix"` and the spec is a single word (`Bash(npm:*)`). The reason is `"allows every <word> subcommand"`.
- **File and Read.**
  - Refuse a bare file tool or bare `Read` at global scope. At other scopes, warn with `"every file in the worktree"`.
  - Refuse when the raw spec starts with `/` or `~`, or contains a `..` segment. The reason is `"paths must be relative to the worktree"`.
  - Refuse when the pattern matches any `_SECRET_PROBES` path under `/probe`: `.env`, `app/.env.local`, `.git/config`, `.claude/settings.json`, `.ssh/id_rsa`, `certs/server.pem`, `certs/server.key`, `credentials.json`. The reason is `"would allow writing secrets or tool config, e.g. <path>"`.
  - Treat the specs `**`, `**/*` and `*` as all-files: refuse at global scope, warn otherwise.
  - Warn when the pattern matches `.github/workflows/ci.yml`: `"can change CI workflows"`.
- **WebFetch.** Refuse `domain:*` or an empty domain. Warn on a bare `WebFetch` (`"any URL"`). Warn on a bare `WebSearch`.
- **MCP.** Warn on a server-wide pattern, i.e. an `mcp__x` with no second `__`: `"every tool on the server"`.
- **Any other bare tool** (`TodoWrite`, `Task`, …) is ok.

- [ ] **Step 1: Failing tests:**

```python
import pytest
from server.automations.autopilot_policy import _BASELINE_DENY, matches_denylist
from server.permission_rules import safety
from server.permission_rules.safety import assess, needs_confirmation, offered_suggestions

@pytest.mark.parametrize("pat,scope,level", [
    ("Bash", "session", "refuse"), ("Bash(*)", "session", "refuse"),
    ("Bash(git *)", "session", "refuse"), ("Bash(git:*)", "repo", "refuse"),
    ("Bash(git push:*)", "session", "refuse"), ("Bash(rm *)", "session", "refuse"),
    ("Bash(rm:*)", "session", "refuse"), ("Bash(sudo:*)", "session", "refuse"),
    ("Bash(git status:*)", "session", "ok"), ("Bash(npm test:*)", "global", "ok"),
    ("Bash(npm:*)", "session", "warn"), ("Bash(npm test)", "session", "ok"),
    ("Edit(src/**)", "repo", "ok"), ("Edit(**)", "session", "warn"),
    ("Edit(**)", "global", "refuse"), ("Edit", "global", "refuse"), ("Edit", "repo", "warn"),
    ("Edit(/etc/**)", "session", "refuse"), ("Edit(../x/**)", "session", "refuse"),
    ("Edit(**/.env*)", "session", "refuse"), ("Edit(.claude/**)", "session", "refuse"),
    ("Edit(.github/**)", "session", "warn"),
    ("WebFetch(domain:*)", "session", "refuse"), ("WebFetch", "session", "warn"),
    ("WebFetch(domain:docs.python.org)", "global", "ok"),
    ("mcp__gh", "session", "warn"), ("mcp__gh__list", "session", "ok"),
    ("Bash(", "session", "refuse"),
])
def test_assess(pat, scope, level):
    assert assess(pat, scope).level == level

def test_probes_cover_the_baseline_denylist():
    hit = {matches_denylist("Bash", {"command": p}, []) for p in safety._DANGER_PROBES}
    assert {n for n, _ in _BASELINE_DENY} - {"secret_exfil"} <= hit

def test_global_always_needs_confirmation():
    assert needs_confirmation(assess("Bash(npm test)", "global"), "global")
    assert not needs_confirmation(assess("Bash(npm test)", "session"), "session")

def test_offered_suggestions_drop_refusals():
    out = offered_suggestions("Bash", {"command": "git push --force"}, "/w")
    assert all(s["level"] != "refuse" for s in out)
    assert [s["pattern"] for s in out] == ["Bash(git push --force)"] or out == []
```

  (`Bash(git push --force)` exact is itself a danger-probe match? `git push --force origin master` does not equal the exact spec, so the exact pattern passes. That is the intended narrowness, and the assertion allows either outcome. If your implementation refuses it, that is fine too.)

- [ ] **Step 2:** Run the tests and expect FAIL.
- [ ] **Step 3:** Implement `safety.py`.
- [ ] **Step 4:** Run the tests and expect green. Then run ruff.
- [ ] **Step 5:** Commit `feat(permission-rules): safety assessment for standing rules`.

---

### Task 3: Storage — scoped auto-profiles + rule stats

**Files:**
- Modify: `server/db/models.py`. Add to `PermissionProfile` (around line 1277): `scope` and `scope_key`, both `Mapped[str | None]` and nullable. Add a new model `PermissionRuleStats` right after it.
- Create: `server/db/migrations/versions/0085_permission_rule_scopes.py` (`down_revision = '0084_session_feedback_resolution'`).
- Modify: `server/tests/test_postgres_compat.py` (`_REQUIRED_EXTRAS`) and `server/tests/test_schema_reconcile_coverage.py` (`RECONCILE_EXEMPT`).
- Create: `server/permission_rules/store.py`
- Modify: `server/api/permission_profiles.py`. Hide scoped profiles from the list, and make create/patch/delete/get refuse them.
- Test: `server/tests/test_permission_rule_store.py`, and extend `server/tests/test_permission_profiles*.py` if one exists. Otherwise add the tests to the store test file using the HTTP harness from `server/tests/test_sessions_actions_api.py` (`create_app` + `TestClient`, `_ENV`).

**Interfaces:**
- Consumes: Task 1 `parse`, `matches_any`; Task 2 `assess`, `SCOPES`.
- Produces (all `async`, all taking an open `AsyncSession db`; the caller commits):
  - `profile_name(scope: str, scope_key: str | None) -> str`. Returns `"always-allow:global"`, `"always-allow:repo:<slug>"` or `"always-allow:session:<id>"`. It validates that `scope_key` is None exactly when the scope is global.
  - `@dataclass class RuleView: id: str; profile_id: str; pattern: str; scope: str; scope_key: str | None; position: int; created_by: str; created_via: str | None; created_at: datetime; hit_count: int; last_hit_at: datetime | None`
    - `def to_wire(self) -> dict` returns camelCase: `id, profileId, pattern, scope, scopeKey, position, createdBy, createdVia, createdAt (iso), hitCount, lastHitAt (iso|None)`.
  - `add_rule(db, *, scope, scope_key, pattern, created_by, created_via) -> RuleView`
    - Gets or creates the auto-profile (`mode=None`, `description="Always-allow rules (<scope>)"`).
    - Appends the pattern to `patterns` if it is absent. If it is present, it returns the existing rule; that is idempotent.
    - Inserts the stats row.
    - On Postgres it locks the profile with `with_for_update()`.
    - It does NOT assess; callers do.
  - `get_rule(db, rule_id) -> RuleView | None`
  - `list_rules(db, *, scopes: list[tuple[str, str | None]] | None = None) -> list[RuleView]`. With None it returns all rules, ordered by scope (`session`, `repo`, `global`), then `scope_key`, then position.
  - `update_rule(db, rule_id, *, new_pattern) -> RuleView | None`. Replaces the pattern in place, keeping its position. If `new_pattern` already exists in the same profile it raises `ValueError("duplicate")`. Moves the stats row to the new pattern, and resets `hit_count`/`last_hit_at`.
  - `revoke_rule(db, rule_id) -> RuleView | None`. Removes the pattern from `patterns` and deletes the stats row. If the profile is left empty, deletes the profile.
  - `applicable_scopes(session_id: str, repo: str | None) -> list[tuple[str, str | None]]` (sync). Returns `[("session", session_id), ("repo", repo) if repo, ("global", None)]`.
  - `applicable_rules(db, session_id, repo) -> list[RuleView]`, most-specific scope first, in position order within a scope.
  - `record_hit(db, rule_id) -> None`. One atomic `UPDATE permission_rule_stats SET hit_count = hit_count + 1, last_hit_at = db_utc_now() WHERE id = :id`.

Model:

```python
class PermissionRuleStats(Base):
    """Metadata for one standing always-allow rule (spec 2026-10-11 §2). The RULE itself is
    the pattern string in its scoped PermissionProfile.patterns — this row only records who
    made it, when, and how often it fired. Its id is the rule's public id."""

    __tablename__ = "permission_rule_stats"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_uuid)
    profile_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    pattern: Mapped[str] = mapped_column(String, nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_via: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("profile_id", "pattern", name="uq_permission_rule_stats_profile_pattern"),)
```

  Check the neighbouring models for whether `Integer` is already imported; add it if not.
  - `RECONCILE_EXEMPT` gains `permission_rule_stats.profile_id`, `.pattern` and `.created_by`, with a comment in the same style as the neighbours (NEW table, mandatory at INSERT).
  - `_REQUIRED_EXTRAS` gains `"permission_rule_stats": {"profile_id": "dtcheck-profile-id", "pattern": "Bash(true)", "created_by": "dtcheck"}`.
  - Read both test files first: if they enumerate tables in some other list (e.g. FK parents), follow that list too.

Migration body: `op.add_column('permission_profiles', sa.Column('scope', sa.String(), nullable=True))`, the same for `scope_key`, and `op.create_table('permission_rule_stats', …)` mirroring the model, with the unique constraint and index. `downgrade` reverses it.

Profile API (`server/api/permission_profiles.py`):
- `list_profiles` filters `PermissionProfile.scope.is_(None)`.
- `get_profile`, `update_profile` and `delete_profile` return 409 `{"error": "managed_profile", "message": "Always-allow rule sets are managed from the rules API"}` when `profile.scope is not None`.
- `create_profile` refuses names starting with `always-allow:` (409 `managed_profile`).

- [ ] **Step 1: Failing store tests** (use the `isolated_db` fixture, plus `from server.db import db_session`):

```python
import asyncio
import pytest
from server.db import db_session
from server.permission_rules import store

pytestmark = pytest.mark.usefixtures("isolated_db")

def _run(coro):
    return asyncio.run(coro)

def test_add_list_revoke_roundtrip():
    async def go():
        async with db_session() as db:
            r = await store.add_rule(db, scope="repo", scope_key="o/r", pattern="Bash(npm test:*)",
                                     created_by="me@x", created_via=None)
        async with db_session() as db:
            rules = await store.applicable_rules(db, "s1", "o/r")
            assert [x.pattern for x in rules] == ["Bash(npm test:*)"]
            assert rules[0].scope == "repo" and rules[0].hit_count == 0
            await store.record_hit(db, r.id)
        async with db_session() as db:
            assert (await store.get_rule(db, r.id)).hit_count == 1
            await store.revoke_rule(db, r.id)
        async with db_session() as db:
            assert await store.applicable_rules(db, "s1", "o/r") == []
    _run(go())

def test_add_is_idempotent_and_scopes_are_isolated():
    async def go():
        async with db_session() as db:
            a = await store.add_rule(db, scope="session", scope_key="s1", pattern="Bash(ls)", created_by="me", created_via=None)
            b = await store.add_rule(db, scope="session", scope_key="s1", pattern="Bash(ls)", created_by="me", created_via=None)
            assert a.id == b.id
            await store.add_rule(db, scope="global", scope_key=None, pattern="WebSearch", created_by="me", created_via=None)
            await store.add_rule(db, scope="repo", scope_key="o/other", pattern="Bash(make)", created_by="me", created_via=None)
        async with db_session() as db:
            got = [(r.scope, r.pattern) for r in await store.applicable_rules(db, "s1", "o/r")]
            assert got == [("session", "Bash(ls)"), ("global", "WebSearch")]
            assert [r.pattern for r in await store.applicable_rules(db, "s2", None)] == ["WebSearch"]
    _run(go())

def test_update_keeps_position_and_rejects_duplicate():
    async def go():
        async with db_session() as db:
            a = await store.add_rule(db, scope="global", scope_key=None, pattern="Bash(a)", created_by="me", created_via=None)
            await store.add_rule(db, scope="global", scope_key=None, pattern="Bash(b)", created_by="me", created_via=None)
            u = await store.update_rule(db, a.id, new_pattern="Bash(a2)")
            assert u.position == 0 and u.pattern == "Bash(a2)"
            with pytest.raises(ValueError):
                await store.update_rule(db, a.id, new_pattern="Bash(b)")
    _run(go())
```

  Add HTTP tests for the profile CRUD guard: a scoped profile is absent from `GET /api/permission-profiles`, gets 409 on PATCH/DELETE, and `POST` with the name `always-allow:x` is 409.
- [ ] **Step 2:** Run and expect FAIL.
- [ ] **Step 3:** Implement the models, migration, store and profile-API guard. Update the two schema lists.
- [ ] **Step 4:** Run `/tmp/vp-test.sh server/tests/test_permission_rule_store.py server/tests/test_schema_reconcile_coverage.py server/tests/test_postgres_compat.py server/tests/test_db_migrations*.py -q`. Glob whatever migration tests exist: `ls server/tests | grep -i migrat`. Expect green.
- [ ] **Step 5:** Commit `feat(permission-rules): store rules in scoped permission profiles`.

---

### Task 4: Enforcement — gate, registry, events, reattach

**Files:**
- Create: `server/permission_rules/gate.py`
- Modify:
  - `server/sessions/permissions.py`. Delete `_grants`, `is_granted`, `GRANT_TTL` and `fingerprint`; add the gate hook; extend `pending()`.
  - `server/sessions/events.py`: add the `PermissionRequest.rule_suggestions` field.
  - `server/sessions/claude_driver.py` (around lines 951–961): pass suggestions into the event.
  - `server/bridge/ws.py` (around lines 1017–1022): the re-emit passes `always_allowed` and `rule_suggestions`.
  - `server/sessions/orchestrator.py` (around line 960): build the gate.
  - `server/config.py`: add `permission_rules_enabled` to BOTH settings classes (the pydantic `Field(default=True, alias="VIBERPIT_PERMISSION_RULES_ENABLED")` near line 290, and the dataclass twin near line 613).
  - `server/api/settings.py`: add a row next to `session_tools_enabled`.
- Test: `server/tests/test_permission_rule_gate.py`. Rewrite the grant-cache tests in `server/tests/test_permissions.py`, `test_permission_registry_external.py` and `test_codex_approvals.py` so they hold under the new semantics: no grant is persisted by `resolve(…, "always")`.

**Interfaces:**
- Consumes:
  - Task 1 `parse`, `matches_any`.
  - Task 2 `offered_suggestions`.
  - Task 3 `store.applicable_rules`, `store.record_hit`, `RuleView`.
  - `server.audit.record_audit(db, type, identity, detail, *, via=None)`.
  - `server.sessions.types.SYSTEM_IDENTITY`.
- Produces:
  - `@dataclass(frozen=True) class RuleHit: rule_id: str; pattern: str; scope: str; scope_key: str | None`
  - `class StandingRuleGate`:
    - `__init__(self, facts: Callable[[str], tuple[str | None, str | None] | None])`. `facts(session_id)` returns `(repo, cwd)`, or None when the session is unknown.
    - `async def match(self, session_id: str, tool: str, input: dict) -> RuleHit | None`. On a hit it records the hit and writes the audit row `permission.rule_allow` `{"sessionId", "ruleId", "pattern", "scope", "tool", "trigger": "request"}` under `SYSTEM_IDENTITY`, `via="rule"`, all in one `db_session()`. It returns None when the kill switch is off, the session is unknown, or **any exception** occurs (`log.warning("permission_rules.gate_failed …", exc_info=True)`; fail closed).
    - `def suggestions(self, session_id: str, tool: str, input: dict) -> list[dict]` (sync). Returns `offered_suggestions(tool, input, cwd)`, or `[]` when the kill switch is off or the session is unknown.
  - Module function `async def first_match(rules: list[RuleView], tool, input, cwd) -> RuleView | None`. It is shared with Task 5, so the edge and the gate match identically. It parses each rule's pattern, skipping an unparsable stored one with a warning, then `matches_any` in rule order and maps the winning `Pattern` back to its `RuleView`.
  - `PermissionRegistry`:
    - `rule_gate: StandingRuleGate | None = None` (attribute, set by the orchestrator).
    - The pending record gains `"suggestions": list[dict]`.
    - `pending()` additionally returns `"always_allowed"` and `"suggestions"`.
    - `request()` returns the same 3-tuple as today.
  - `PermissionRequest(..., rule_suggestions: list[dict] | None = None)`. The wire key is `"ruleSuggestions"`, and it carries `[]` when None.

Registry changes (`server/sessions/permissions.py`):
1. Delete `GRANT_TTL`, `self._grants`, `fingerprint()` and `is_granted()`. Grep the repo for `is_granted`, `_grants` and `fingerprint(` (server only) and remove every user.
2. In `resolve()`, `"always"` on a non-raw record now just means `"allow"`. Persist nothing. Update the comment: the edge turned the "always" into a standing rule before calling here (spec §3.4).
3. Add `async def _rule_allows(self, session_id, tool, input) -> bool`. It returns `self.rule_gate is not None and await self.rule_gate.match(session_id, tool, input) is not None`.
4. In the MUTATION branch, replace the `is_granted` check with `if await self._rule_allows(session_id, tool, input): return None, _resolved(True), True`.
5. Delete the generic grant-cache line (`if mode != "interactive" and allowlist and self.is_granted(...)`). Immediately before the final `_pend` (after the `acceptEdits` branch), add:
   ```python
   if tool not in _NEVER_RULED and await self._rule_allows(session_id, tool, input):
       return None, _resolved(True), True
   ```
   with `_NEVER_RULED = frozenset({"ExitPlanMode", "AskUserQuestion"})` at module level. Add a comment explaining that it is checked in every mode, including interactive, because a standing rule is an explicit human decision (spec §3.1).
6. In `_pend`, compute `suggestions = self.rule_gate.suggestions(session_id, tool, input) if (self.rule_gate and always_allowed and tool not in _NEVER_RULED) else []` and store it on the record. Make `_pend` return `(pid, fut)` as today. Callers read suggestions via a new `def suggestions_for(self, perm_id) -> list[dict]`. That keeps the `request()` tuple stable for every engine.
7. `pending()` adds `"always_allowed": r.get("always_allowed", True)` and `"suggestions": r.get("suggestions") or []`.

Driver (`claude_driver.py`): when `perm_id` is not None, build the event with `always_allowed=always_allowed` (already there) and `rule_suggestions=self._reg.suggestions_for(perm_id)`.

ws.py reattach: add `always_allowed=p.get("always_allowed", True), rule_suggestions=p.get("suggestions") or []` to the `PermissionRequest(...)` call.

Orchestrator: right after `self._registry = PermissionRegistry()`, add
```python
from server.permission_rules.gate import StandingRuleGate
self._registry.rule_gate = StandingRuleGate(self._rule_facts)
```
plus the method
```python
def _rule_facts(self, session_id: str) -> tuple[str | None, str | None] | None:
    """(repo, cwd) a standing-rule check needs (spec 2026-10-11 §3.2); None = unknown."""
    rec = self._records.get(session_id)
    return None if rec is None else (rec.repo, rec.cwd)
```
Place the import with the other module-level imports if that does not create a cycle. Otherwise keep it local, with a comment.

- [ ] **Step 1: Failing tests** in `test_permission_rule_gate.py` (`isolated_db`):

```python
import asyncio
import pytest
from sqlalchemy import select
from server.db import db_session
from server.db.models import AuditEvent
from server.permission_rules import store
from server.permission_rules.gate import StandingRuleGate
from server.sessions.permissions import PermissionRegistry

pytestmark = pytest.mark.usefixtures("isolated_db")
FACTS = {"s1": ("o/r", "/w/repo"), "s2": (None, "/w/scratch")}

def _reg():
    reg = PermissionRegistry()
    reg.rule_gate = StandingRuleGate(FACTS.get)
    return reg

async def _add(scope, key, pattern):
    async with db_session() as db:
        return await store.add_rule(db, scope=scope, scope_key=key, pattern=pattern,
                                    created_by="me", created_via=None)

@pytest.mark.parametrize("mode", ["interactive", "auto", "allowlist", "acceptEdits"])
def test_repo_rule_auto_allows_in_every_prompting_mode(mode):
    async def go():
        r = await _add("repo", "o/r", "Bash(npm test:*)")
        reg = _reg()
        pid, fut, _ = await reg.request("s1", "t", "Bash", {"command": "npm test -w"}, mode=mode,
                                        denylist=PermissionRegistry.DANGEROUS, allowlist=True, identity="me")
        assert pid is None and fut.result() is True
        async with db_session() as db:
            assert (await store.get_rule(db, r.id)).hit_count == 1
            rows = (await db.execute(select(AuditEvent).where(AuditEvent.type == "permission.rule_allow"))).scalars().all()
            assert len(rows) == 1
        # other repo / scratch session: still cards
        pid2, _, _ = await reg.request("s2", "t", "Bash", {"command": "npm test"}, mode="interactive",
                                       denylist=(), allowlist=True, identity="me")
        assert pid2 is not None
    asyncio.run(go())

def test_revoke_takes_effect_on_next_request():
    async def go():
        r = await _add("session", "s1", "Bash(ls)")
        reg = _reg()
        assert (await reg.request("s1", "t", "Bash", {"command": "ls"}, mode="interactive",
                                  denylist=(), allowlist=True, identity="me"))[0] is None
        async with db_session() as db:
            await store.revoke_rule(db, r.id)
        assert (await reg.request("s1", "t", "Bash", {"command": "ls"}, mode="interactive",
                                  denylist=(), allowlist=True, identity="me"))[0] is not None
    asyncio.run(go())

def test_gate_fails_closed(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr("server.permission_rules.store.applicable_rules", boom)
    async def go():
        reg = _reg()
        pid, _, _ = await reg.request("s1", "t", "Bash", {"command": "ls"}, mode="interactive",
                                      denylist=(), allowlist=True, identity="me")
        assert pid is not None
    asyncio.run(go())

def test_plan_and_destructive_never_rule_resolved():
    async def go():
        await _add("global", None, "ExitPlanMode")
        reg = _reg()
        pid, _, _ = await reg.request("s1", "t", "ExitPlanMode", {}, mode="interactive",
                                      denylist=(), allowlist=True, identity="me")
        assert pid is not None
    asyncio.run(go())

def test_pending_carries_suggestions_and_always_allowed():
    async def go():
        reg = _reg()
        pid, _, _ = await reg.request("s1", "t", "Bash", {"command": "npm test"}, mode="interactive",
                                      denylist=(), allowlist=True, identity="me")
        [p] = reg.pending("s1")
        assert p["always_allowed"] is True
        assert p["suggestions"][0]["pattern"] == "Bash(npm test)"
        assert reg.suggestions_for(pid) == p["suggestions"]
    asyncio.run(go())

def test_kill_switch_disables_gate(monkeypatch):
    monkeypatch.setenv("VIBERPIT_PERMISSION_RULES_ENABLED", "false")
    async def go():
        await _add("session", "s1", "Bash(ls)")
        reg = _reg()
        assert (await reg.request("s1", "t", "Bash", {"command": "ls"}, mode="interactive",
                                  denylist=(), allowlist=True, identity="me"))[0] is not None
    asyncio.run(go())
```

  Also add a destructive-ViberPit-tool case. Find a DESTRUCTIVE tool name in `server/assistant/tools.py` (grep `ToolClass.DESTRUCTIVE`), add a global rule for it, and assert it still cards with `always_allowed` False and `suggestions == []`. Check how `get_settings()` caches (`server/config.py:395`) so the kill-switch test actually takes effect. If settings are cached, read the env var through the same accessor other kill switches use, and make the test follow that pattern.
- [ ] **Step 2:** Run and expect FAIL.
- [ ] **Step 3:** Implement. Then rewrite the old grant-cache tests:
  - `test_permissions.py::test_always_grant_is_fingerprinted_and_expires` and `test_bash_always_grant_does_not_cover_other_commands` become a single test: `resolve(pid, "always")` allows this call, and the next identical request still cards (no rule exists).
  - The two interactive "always" tests keep their assertion (the second request cards).
  - In the Codex tests, keep the raw-decision passthrough assertions and drop any `_grants` introspection.
- [ ] **Step 4:** Run `/tmp/vp-test.sh server/tests/test_permission_rule_gate.py server/tests/test_permissions.py server/tests/test_permission_registry_external.py server/tests/test_codex_approvals.py server/tests/test_permissions_session_tools.py server/tests/test_ws*.py -q`. Also run `grep -rln "PermissionRequest(" server/tests` and run those files. Expect green. Run ruff and mypy on the touched files.
- [ ] **Step 5:** Commit `feat(permission-rules): enforce standing rules in the permission registry`.

---

### Task 5: Edge — human-only guard, grant-from-card, rules API

**Files:**
- Modify: `server/sessions/session_actions.py`
- Create: `server/api/permission_rules.py`. Register it in `server/app.py` next to `permission_profiles_router` (around lines 52 and 981).
- Test: `server/tests/test_permission_rules_api.py`. Update `server/tests/test_session_actions.py` and `server/tests/test_sessions_actions_api.py` (the "always" tests).

**Interfaces:**
- Consumes:
  - Tasks 1–4. Notably `gate.first_match`, `store.*`, `safety.assess` / `needs_confirmation` / `offered_suggestions`, and `patterns.parse` / `matches_any` / `describe`.
  - The orchestrator facade methods `pending_permissions(session_id) -> list[dict]` (now carrying `suggestions`, `always_allowed`) and `resolve_permission(session_id, perm_id, decision, identity) -> bool`, plus `get_session(session_id)` for `repo` and `cwd`. Check `server/sessions/facade.py` for the exact accessor that returns a record with `.repo` and `.cwd` on both the local and the remote orchestrator. If none exists remotely, read `repo` and `cwd` from the DB `Session` row (`server.db.models.Session`: `repo`, `cwd`).
- Produces:
  - `session_actions.RULE_HUMAN_ONLY_CODE = "RULES_HUMAN_ONLY"`
  - `session_actions.require_human(identity: str, via: str | None) -> None`. Raises `ActionError(RULES_HUMAN_ONLY, …, 403)` unless `via is None and identity != SYSTEM_IDENTITY`.
  - `async session_actions.grant_always(orch, db, *, session_id, perm_id, pattern, scope, confirm, identity, via) -> dict`. Returns `{"rule": RuleView.to_wire(), "resolvedPermIds": [...]}`.
  - The existing `resolve_permission` now handles `"always"` as follows:
    - Call `require_human` first. This replaces the `via == "assistant"` check. Keep the old error code `ALWAYS_FORBIDDEN_VIA_ASSISTANT` (400) **only** for `via == "assistant"` (existing clients and tests rely on it). Any other non-human via gets `RULES_HUMAN_ONLY` 403.
    - Look up the pending record via `orch.pending_permissions`.
    - If it is engine-managed (its `suggestions` are empty because the record is raw), pass `"always"` through unchanged; that is Codex's `acceptForSession`.
    - Otherwise call `grant_always` with the first suggestion whose level is `ok`, scope `"session"` and `confirm=False`. If there is no ok suggestion, resolve with `"allow"`.
    - Detect engine-managed records by adding `"engine_managed": bool(r.get("raw_decision"))` to `PermissionRegistry.pending()`. That is a small Task-4-adjacent edit; make it here and test it.

`grant_always` algorithm (spec §3.3, §5):
1. `require_human`. If the kill switch is off: `ActionError("RULES_DISABLED", …, 404)`.
2. `scope` must be in `SCOPES`, else 400 `INVALID_SCOPE`. Find the pending record: `p = next(p for p in await orch.pending_permissions(session_id) if p["permId"] == perm_id)`, else 404 `PERMISSION_NOT_PENDING`. If `not p.get("always_allowed", True)` or `p.get("engine_managed")`: 400 `RULE_NOT_ALLOWED`.
3. Load the session's `repo` and `cwd`. A `repo` scope with no repo is 400 `NO_REPO`. The scope key is `session_id`, `repo`, or None.
4. `a = assess(pattern, scope)`. On `refuse`: 400 `RULE_REFUSED`, with `reasons`. If `needs_confirmation(a, scope) and not confirm`: 409 `RULE_NEEDS_CONFIRMATION`, with `level`, `reasons`. Put the extra fields on `ActionError` as an optional `extra: dict`, and make the HTTP layer merge it into `detail`. Check how `ActionError` is rendered in `server/api/sessions.py`.
5. `matches_any([parse(pattern)], p["tool"], p["input"], cwd)` must be non-None, else 400 `PATTERN_DOES_NOT_COVER`.
6. `rule = await store.add_rule(db, scope=..., scope_key=..., pattern=..., created_by=identity, created_via=via)`. Then `record_audit(db, "permission_rule.create", identity, {"ruleId", "pattern", "scope", "scopeKey", "sessionId", "permId"}, via=via)`, and `await db.commit()`.
7. Resolve the originating card: `await orch.resolve_permission(session_id, perm_id, "allow", identity)`, plus `note_user_interaction`. On False → 403/404, same as `resolve_permission`.
8. Sweep the other pending cards in scope:
   - Candidate session ids: for `session` scope just `[session_id]`. Otherwise `select(Session.id, Session.repo, Session.cwd).where(Session.status == "waiting_input")`, filtered by `Session.repo == repo` for repo scope. Check `server/db/models.py` for the exact status/archived column names, and exclude archived sessions.
   - For each, call `orch.pending_permissions(sid)`. For each card that is not the original and has `always_allowed` true, check whether `matches_any([parse(pattern)], tool, input, that_session_cwd)` matches. Engine-managed (Codex) cards ARE included: they are resolved once with `"allow"`. If it matches: `ok = await orch.resolve_permission(sid, pid, "allow", identity)`.
   - If ok, append the pid and audit `permission.rule_allow` with `trigger: "grant"`.
   - A failure in one session (an exception, e.g. `DaemonUnavailable`) is logged with a warning and skipped.
9. Return `{"rule": rule.to_wire(), "resolvedPermIds": [perm_id, *swept]}`.

Routes (`server/api/permission_rules.py`). Pattern your error rendering on how `server/api/sessions.py` turns `ActionError` into `HTTPException(status_code=e.http, detail={"code": e.code, "message": e.message, **e.extra})`. Get `via` from `request.state.via` (`getattr(request.state, "via", None)`), the same way the session routes do; grep `state.via` in `server/api/sessions.py`.
- `POST /api/sessions/{session_id}/permissions/{perm_id}/always`, body `{pattern: str, scope: str, confirm: bool = False}`. It calls `grant_always`. Put this route in `permission_rules.py` with `APIRouter()` and a full path; check that it does not collide with the existing `POST /api/sessions/{id}/permissions/{perm_id}` route (the extra `/always` segment makes it distinct).
- `POST /api/permission-rules/preview`, body `{sessionId: str, permId: str | None, pattern: str, scope: str}`. It requires a human (403 otherwise; it reveals nothing sensitive, but keep the surface uniform). It returns:
  ```
  {valid: bool, level, reasons: [...], description: str|None,
   needsConfirmation: bool, coversRequest: bool|None, alsoPending: int}
  ```
  `alsoPending` is the number of OTHER pending cards in that session (only that session, cheap) that the pattern would match.
- `GET /api/permission-rules?sessionId=`. Any authenticated identity may read. With `sessionId` it returns `store.applicable_rules` for that session's repo; otherwise `store.list_rules(db)`. It returns `[RuleView.to_wire()]`.
- `PATCH /api/permission-rules/{rule_id}`, body `{pattern: str, confirm: bool = False}`. Requires a human. Runs `assess(pattern, rule.scope)` with the same 400/409 logic. Calls `store.update_rule` (ValueError → 409 `DUPLICATE_RULE`). Audits `permission_rule.update`. Returns the view.
- `DELETE /api/permission-rules/{rule_id}`. Requires a human. Calls `store.revoke_rule`; None → 404. Audits `permission_rule.revoke`. Returns 204.
- Every route returns 404 when the kill switch is off.

- [ ] **Step 1: Failing tests.** Use `test_sessions_actions_api.py`'s harness: copy its `_FakeSessions` shape into the new test file as a local fake. It needs `pending_permissions`, `resolve_permission`, `note_user_interaction` and `get_session` / record lookup. Seed real rule rows through the app's DB, which the lifespan initialises, via `server.db.db_session`. Cases:
  - `test_grant_from_card_creates_rule_resolves_card_and_sweeps_matching_pending`: the fake returns two pending cards in s1 (`npm test -w`, `rm x`) and one in s2 (same repo, `npm test`). Granting `Bash(npm test:*)` at repo scope resolves the original and s2's card, but NOT `rm x`. A `GET /api/permission-rules` then shows the rule with `createdBy == "testuser@example.com"`.
  - `test_grant_pattern_must_cover_request` → 400 `PATTERN_DOES_NOT_COVER`, and no rule is stored.
  - `test_grant_refused_pattern` (`Bash(git *)`) → 400 `RULE_REFUSED`, with reasons.
  - `test_grant_warn_needs_confirm` (`Bash(npm:*)`) → 409, then 200 with `confirm: true`.
  - `test_global_scope_needs_confirm` → 409 without `confirm`.
  - `test_assistant_token_cannot_grant`: mint a token as in `test_permission_always_forbidden_via_assistant`, POST `/always` → 403 `RULES_HUMAN_ONLY`, and no rule is stored. Also PATCH/DELETE `/api/permission-rules/{id}` with the token → 403. Additionally assert that `server.assistant.policy.route_allowed("POST", "/api/sessions/<uuid>/permissions/p1/always")` is False, and the same for `POST /api/permission-rules/preview`, `PATCH /api/permission-rules/x` and `DELETE /api/permission-rules/x`.
  - `test_legacy_always_creates_session_rule`: POST the existing route with `{"decision":"always"}` as a human. A session-scope rule with the narrowest ok suggestion now exists, and the fake saw `resolve_permission(..., "allow", ...)`.
  - `test_legacy_always_engine_managed_passes_through`: the pending card has `engine_managed: True` → the fake saw `"always"`, and no rule exists.
  - `test_patch_and_delete_rule` (round trip, plus audit rows `permission_rule.update` and `permission_rule.revoke`).
  - `test_session_actions.py`: update `test_resolve_permission_always_forbidden_via_assistant_before_orchestrator_call` (it still holds). Add `via="mcp-external"` → `RULES_HUMAN_ONLY`.
- [ ] **Step 2:** Run and expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `/tmp/vp-test.sh server/tests/test_permission_rules_api.py server/tests/test_session_actions.py server/tests/test_sessions_actions_api.py server/tests/test_assistant_conformance.py server/tests/test_assistant_tools.py -q`. Expect green. Run ruff and mypy.
- [ ] **Step 5:** Commit `feat(permission-rules): human-only grant-from-card and rules API`.

---

### Task 6: Docs, assistant copy, full gate

**Files:**
- Modify: `docs/product/features/session-permissions.md`. Add a **Standing rules** note covering:
  - grammar;
  - scopes and their fleet-wide reach;
  - the human-only rule;
  - the safety tiers;
  - enforcement in every mode;
  - the kill switch;
  - the removal of the 1-hour grant cache.

  Add `server/permission_rules/` and `server/api/permission_rules.py` to `links.code`. Fix the Non-goals sentence "Tool-pattern strings in profiles are stored as-is … not server-validated": it is now true only for *named* profiles.
- Modify: `server/assistant/tools.py`, the `resolve_permission` tool description. State that it is allow/deny only and that always-allow rules are human-only. Leave the `create_session` "not yet enforced" sentence: named profiles are still unenforced. Run the assistant conformance and eval fixture tests after any description change (`server/tests/test_assistant_conformance.py`, `test_eval_assistant_fixtures.py`).

Steps:
- [ ] **Step 1:** Make the doc and copy edits.
- [ ] **Step 2:** Run the full four-part gate, in the background with a log. Never start two suites at once.
  ```bash
  /home/tony/GitHub/viberpit/.venv/bin/ruff check src/ server/ 2>&1 | tee /tmp/ar-ruff.log
  /home/tony/GitHub/viberpit/.venv/bin/mypy server/ --ignore-missing-imports 2>&1 | tail -3 | tee /tmp/ar-mypy.log
  /tmp/vp-test.sh tests/ -q -n 4 2>&1 | tail -5 | tee /tmp/ar-core.log
  /tmp/vp-test.sh server/tests/ -q -n auto --dist loadfile --timeout=300 2>&1 | tee /tmp/ar-server.log | tail -30
  ```
  Expect green. Treat any failure as real until it is proven pre-existing on a detached `origin/master` run.
- [ ] **Step 3:** Run `python scripts/brand_ratchet.py` and expect it to pass (new code must not spell the legacy brand).
- [ ] **Step 4:** Commit `docs(permission-rules): feature doc and assistant copy`.

---

## Self-review notes

- **Spec coverage:**
  - §1 → T1. §2 → T3. §3.1–3.2 → T4. §3.3 → T5 step 8. §3.4 → T4 and T5.
  - §4 → T2, plus the human-only part in T5.
  - §5 → T5. The event field is T4.
  - §6 web → PR 2 and PR 3 (separate plans).
  - §7 → the tests across T1–T5. §8 → T4 (flag) and T5 (routes 404).
- **Correction recorded in T5 step 8:** Codex (engine-managed) cards are swept with `"allow"`, but a rule cannot be *created* from one.
