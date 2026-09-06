# ViberPit Rebrand — Phase 0a + Phase 1a/1b Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the brand one source of truth in Python and one in the web app, route every display string, MCP prefix, and host-path literal through it (behaviour-neutral), then flip the display name to **ViberPit** on every external surface that carries no compatibility contract.

**Architecture:** `src/ccswitch/brand.py` and `web/src/brand.ts` own the name forms. Wire contracts that must not change yet (MCP tool prefix, host paths, CLI binary name) are separate constants pinned to the legacy slug with a comment naming the phase that flips them. `server/hostpaths.py` collapses the four modules that each spelled `/run/ccswitch`, `/var/lib/ccswitch`, `/var/log/ccswitch`. PR A (Tasks 1–6) changes no observable string; PR B (Tasks 7–8) flips the display name and the text posted to GitHub.

**Tech Stack:** Python 3.11 (FastAPI server + `ccswitch` CLI package), pytest; React + Vite + TypeScript, vitest.

**Spec:** `docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md` (§1 name forms, §3 Phases 0a/1a/1b, §4 risks 2 and 8, §5 testing).

## Global Constraints

- Display name `ViberPit`; slug `viberpit`; legacy slug `ccswitch` (spec §1).
- Every brand string derives from `brand.py` / `brand.ts`; no other module spells the brand (spec §1).
- These stay on the legacy value in this plan: MCP server name and `mcp__ccswitch__` prefix (Phase 2), all host paths (Phase 3), the CLI console-script name and its message prefixes (Phase 5), localStorage keys, cookie, headers, markers, branch prefix, `.ccswitch/` namespace, env var names.
- Deviation from spec §3 Phase 1a, decided here: CLI message prefixes (`ccswitch crypto: …`) flip with the binary in Phase 5, because a prefix naming a binary you cannot yet run is worse than the old name. Phase 0b (env mirror) moves to the plan that does the env-var sweep so it lands with its first consumer (constitution VI, no dead code).
- Run Python tests with the shared venv **and** `PYTHONPATH=$PWD/src` (the venv's editable `.pth` resolves `ccswitch.*` from the deploy checkout, so a new `ccswitch.brand` module is invisible without it). Strip all `CCSWITCH_*` env before `server/tests/`:
  `for v in $(env | grep -o '^CCSWITCH_[A-Z0-9_]*'); do unset $v; done`.
- Four-step gate before every push: `ruff check src/ server/`, `mypy server/ --ignore-missing-imports`, `pytest tests/`, `pytest server/tests/ -n auto --dist loadfile --timeout=300` (background, tee to `/tmp/`). Web: `(cd web && npm run typecheck && npm run lint && npm run test && npm run build)`; then `rm -rf web/dist` before any further pytest.
- Bash hook: never put a deploy verb and a prod word in one command's text; commit messages go through `-F <file>`.
- Branch off `origin/master` after `git fetch`; commit trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; PR ready + `gh pr merge --squash --auto`.

---

## PR A — Phase 0a: brand source of truth (behaviour-neutral)

Branch: `rebrand/phase-0a-brand-source-of-truth`. Proof of neutrality: the existing suites pass unchanged, plus the new invariant tests.

### Task 1: `ccswitch.brand` module

**Files:**
- Create: `src/ccswitch/brand.py`
- Test: `tests/test_brand.py`

**Interfaces:**
- Produces (used by every later task):
  `brand.NAME: str`, `brand.SLUG: str`, `brand.LEGACY_SLUG: str`, `brand.CLI_NAME: str`,
  `brand.HOST_SLUG: str`, `brand.MCP_SERVER_NAME: str`, `brand.MCP_TOOL_PREFIX: str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_brand.py
"""brand.py is the only module that spells the product name. These invariants are
what every later rename phase relies on."""
import re

from ccswitch import brand

_SLUG = re.compile(r"^[a-z][a-z0-9]*$")


def test_slug_is_lowercase_name():
    assert brand.SLUG == brand.NAME.lower()


def test_slugs_are_identifier_safe():
    for value in (brand.SLUG, brand.LEGACY_SLUG, brand.CLI_NAME, brand.HOST_SLUG,
                  brand.MCP_SERVER_NAME):
        assert _SLUG.match(value), value


def test_mcp_prefix_derives_from_server_name():
    # The SDK names tools `mcp__<server>__<tool>`; the prefix must never be typed by hand.
    assert brand.MCP_TOOL_PREFIX == f"mcp__{brand.MCP_SERVER_NAME}__"


def test_legacy_slug_is_frozen():
    # Compat readers key on this forever; it must not follow SLUG when SLUG flips.
    assert brand.LEGACY_SLUG == "ccswitch"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_brand.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ccswitch.brand'`

- [ ] **Step 3: Write the module**

```python
# src/ccswitch/brand.py
"""Brand identity — the ONE place the product name is spelled.

Display strings, User-Agents, notification titles, and filenames derive from
``NAME`` / ``SLUG``. Wire contracts that cannot change without a dual-accept
window are pinned to ``LEGACY_SLUG`` here and flip in their own phase (see
docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md §3). Nothing outside
this module may spell the brand.
"""
from __future__ import annotations

#: Display name — UI, page titles, prose, push-notification titles, GitHub comments.
NAME = "ccswitch"
#: Lowercase identifier stem — User-Agent product, download filenames, new slugs.
SLUG = "ccswitch"
#: The pre-rebrand slug. Only compat readers (parsers, path fallbacks) may use it.
LEGACY_SLUG = "ccswitch"

#: The installed console script. CLI message prefixes name the binary the user ran,
#: so this flips in Phase 5 when the ``viberpit`` entry point is added.
CLI_NAME = LEGACY_SLUG
#: Stem of /etc, /run, /var/lib, /var/log paths and systemd units. Flips in Phase 3
#: together with the unit files and the per-host migration runbook.
HOST_SLUG = LEGACY_SLUG
#: MCP server wire name. The SDK derives tool names ``mcp__<name>__<tool>`` from it,
#: and the permission registry string-matches the prefix, so it flips in Phase 2
#: with a dual-accept window — never on its own.
MCP_SERVER_NAME = LEGACY_SLUG
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_brand.py -v`
Expected: 4 passed. Confirm the import resolved to your checkout:
`PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -c "import ccswitch.brand as m; print(m.__file__)"` prints a path under this worktree.

- [ ] **Step 5: Commit**

```bash
git add src/ccswitch/brand.py tests/test_brand.py
printf 'feat(brand): add ccswitch.brand as the single source of truth for the product name\n\nRefs spec docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md §1.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c1.txt
git commit -F /tmp/c1.txt
```

---

### Task 2: One MCP prefix (server)

**Files:**
- Modify: `server/assistant/tools.py:24`
- Modify: `server/sessions/events.py:34`
- Modify: `server/sessions/claude_driver.py:695`
- Modify: `server/sessions/permissions.py:138`
- Modify: `server/assistant/server.py:27`
- Modify: `server/assistant/options.py:65`
- Test: `server/tests/test_brand_wire_contracts.py`

**Interfaces:**
- Consumes: `brand.MCP_TOOL_PREFIX`, `brand.MCP_SERVER_NAME` (Task 1).
- Produces: `server.assistant.tools.MCP_PREFIX` and `server.sessions.events._APP_ACTION_PREFIX` remain importable names but are aliases of `brand.MCP_TOOL_PREFIX`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_brand_wire_contracts.py
"""The MCP tool prefix gates a fail-closed permission registry (spec §4 risk 2).
Five hand-typed copies once existed; this pins them to one constant so the Phase 2
flip is a one-line change that cannot desynchronise the server name from a matcher."""
from pathlib import Path

from ccswitch import brand
from server.assistant import tools
from server.sessions import events

SERVER_ROOT = Path(__file__).resolve().parents[1]
_STRAY = ('"mcp__ccswitch__"', "'mcp__ccswitch__'",
          'create_sdk_mcp_server(name="ccswitch"', '{"ccswitch": server}')


def test_prefix_constants_are_one_value():
    assert tools.MCP_PREFIX == brand.MCP_TOOL_PREFIX
    assert events._APP_ACTION_PREFIX == brand.MCP_TOOL_PREFIX
    assert brand.MCP_TOOL_PREFIX == f"mcp__{brand.MCP_SERVER_NAME}__"


def test_no_hand_typed_prefix_in_server_code():
    offenders = []
    for path in SERVER_ROOT.rglob("*.py"):
        if "tests" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if any(s in line for s in _STRAY):
                offenders.append(f"{path.relative_to(SERVER_ROOT)}:{lineno}")
    assert offenders == [], offenders
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_brand_wire_contracts.py -v`
Expected: `test_no_hand_typed_prefix_in_server_code` FAILS listing the six sites above.

- [ ] **Step 3: Route each site through `brand`**

`server/assistant/tools.py:24` — replace `MCP_PREFIX = "mcp__ccswitch__"` with:
```python
from ccswitch import brand

MCP_PREFIX = brand.MCP_TOOL_PREFIX
```
(put the import with the other `ccswitch` imports at the top of the file).

`server/sessions/events.py:34` — replace `_APP_ACTION_PREFIX = "mcp__ccswitch__"` with:
```python
from ccswitch import brand

_APP_ACTION_PREFIX = brand.MCP_TOOL_PREFIX
```

`server/sessions/claude_driver.py:695` — replace `tool_name.startswith("mcp__ccswitch__")` with `tool_name.startswith(brand.MCP_TOOL_PREFIX)`; add `from ccswitch import brand` at the top.

`server/sessions/permissions.py:138` — replace `tool.startswith("mcp__ccswitch__")` with `tool.startswith(brand.MCP_TOOL_PREFIX)`; add the import.

`server/assistant/server.py:27` — `create_sdk_mcp_server(name=brand.MCP_SERVER_NAME, version=…, tools=tools)`; add the import.

`server/assistant/options.py:65` — `"mcp_servers": {brand.MCP_SERVER_NAME: server},`; add the import.

Leave comments and docstrings that mention `mcp__ccswitch__` in backticks alone; the scan only matches quoted literals.

- [ ] **Step 4: Run the new test and the touched suites**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_brand_wire_contracts.py server/tests/test_assistant_permissions.py server/tests/test_permissions.py server/tests/test_assistant_codex_overlay.py -v --timeout=120`
Expected: all PASS (values unchanged, so the 12 existing tests that pin `mcp__ccswitch__` still pass).

- [ ] **Step 5: Commit**

```bash
git add server/assistant/tools.py server/sessions/events.py server/sessions/claude_driver.py server/sessions/permissions.py server/assistant/server.py server/assistant/options.py server/tests/test_brand_wire_contracts.py
printf 'refactor(mcp): derive the mcp__ccswitch__ prefix from brand.MCP_SERVER_NAME everywhere\n\nFive hand-typed copies gated a fail-closed permission registry; one constant now.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c2.txt
git commit -F /tmp/c2.txt
```

---

### Task 3: `server/hostpaths.py`

**Files:**
- Create: `server/hostpaths.py`
- Modify: `server/ipc/paths.py:13`
- Modify: `server/deploy/serve.py:13,18`
- Modify: `server/api/deploy.py:19`
- Modify: `server/sessions/deploy_snapshot.py:5`
- Modify: `server/sessions/daemon_health.py:36`
- Modify: `server/config.py:199,492`
- Modify: `server/sessions/push.py:225`
- Test: `server/tests/test_hostpaths.py`

**Interfaces:**
- Consumes: `brand.HOST_SLUG` (Task 1).
- Produces: `hostpaths.RUN_DIR, SESSIOND_SOCKET, DEPLOY_SOCKET, DEPLOY_STATUS_PATH, STATE_DIR, HEALTH_STATE_DIR, DEPLOY_SNAPSHOT_PATH, LOG_DIR, ETC_DIR, ENV_FILE` (all `str`). Existing names (`server.ipc.paths.DEFAULT_SOCKET_PATH`, `server.deploy.serve.DEFAULT_DEPLOY_SOCKET`, `DEFAULT_DEPLOY_STATUS_PATH`, `server.sessions.deploy_snapshot.DEFAULT_PATH`, `daemon_health._DEFAULT_HEALTH_STATE_DIR`) stay importable as aliases.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_hostpaths.py
"""Host paths were spelled in four modules; Phase 3 renames them on every host, so
they must come from one place and, until then, equal the literals the units use."""
from ccswitch import brand
from server import hostpaths
from server.deploy import serve
from server.ipc import paths as ipc_paths
from server.sessions import daemon_health, deploy_snapshot


def test_values_match_the_installed_units():
    # deploy/*.service: RuntimeDirectory=ccswitch, StateDirectory=ccswitch,
    # EnvironmentFile=/etc/ccswitch/ccswitch.env
    assert hostpaths.SESSIOND_SOCKET == "/run/ccswitch/sessiond.sock"
    assert hostpaths.DEPLOY_SOCKET == "/run/ccswitch/deploy.sock"
    assert hostpaths.DEPLOY_STATUS_PATH == "/run/ccswitch/deploy-status.json"
    assert hostpaths.HEALTH_STATE_DIR == "/var/lib/ccswitch/health"
    assert hostpaths.DEPLOY_SNAPSHOT_PATH == "/var/lib/ccswitch/deploy-snapshot/live.json"
    assert hostpaths.LOG_DIR == "/var/log/ccswitch"
    assert hostpaths.ENV_FILE == "/etc/ccswitch/ccswitch.env"


def test_derived_from_host_slug():
    for value in (hostpaths.RUN_DIR, hostpaths.STATE_DIR, hostpaths.LOG_DIR, hostpaths.ETC_DIR):
        assert value.endswith("/" + brand.HOST_SLUG), value


def test_consumers_alias_hostpaths():
    assert ipc_paths.DEFAULT_SOCKET_PATH is hostpaths.SESSIOND_SOCKET
    assert serve.DEFAULT_DEPLOY_SOCKET is hostpaths.DEPLOY_SOCKET
    assert serve.DEFAULT_DEPLOY_STATUS_PATH is hostpaths.DEPLOY_STATUS_PATH
    assert deploy_snapshot.DEFAULT_PATH is hostpaths.DEPLOY_SNAPSHOT_PATH
    assert daemon_health._DEFAULT_HEALTH_STATE_DIR is hostpaths.HEALTH_STATE_DIR
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_hostpaths.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server.hostpaths'`

- [ ] **Step 3: Create the module and point the consumers at it**

```python
# server/hostpaths.py
"""Well-known host paths — the ONE place the /etc, /run, /var/lib, /var/log stems are
spelled. They mirror the systemd units in deploy/ (RuntimeDirectory=, StateDirectory=,
EnvironmentFile=) and flip with them in rebrand Phase 3 via ``brand.HOST_SLUG``.
Env overrides (``CCSWITCH_DEPLOY_SOCKET`` …) stay at the call sites; these are defaults."""
from __future__ import annotations

from ccswitch import brand

RUN_DIR = f"/run/{brand.HOST_SLUG}"
SESSIOND_SOCKET = f"{RUN_DIR}/sessiond.sock"
DEPLOY_SOCKET = f"{RUN_DIR}/deploy.sock"
DEPLOY_STATUS_PATH = f"{RUN_DIR}/deploy-status.json"

STATE_DIR = f"/var/lib/{brand.HOST_SLUG}"
HEALTH_STATE_DIR = f"{STATE_DIR}/health"
DEPLOY_SNAPSHOT_PATH = f"{STATE_DIR}/deploy-snapshot/live.json"

LOG_DIR = f"/var/log/{brand.HOST_SLUG}"

ETC_DIR = f"/etc/{brand.HOST_SLUG}"
ENV_FILE = f"{ETC_DIR}/{brand.HOST_SLUG}.env"
```

Consumers (keep the old names as aliases so nothing else moves):

- `server/ipc/paths.py:13` → `from server.hostpaths import SESSIOND_SOCKET as DEFAULT_SOCKET_PATH`
- `server/deploy/serve.py:13,18` → `from server.hostpaths import DEPLOY_SOCKET as DEFAULT_DEPLOY_SOCKET, DEPLOY_STATUS_PATH as DEFAULT_DEPLOY_STATUS_PATH` (keep the comment block above line 18; edit its `/run/ccswitch` mentions in the docstrings at lines 23 and 29 to say "the default under `hostpaths.RUN_DIR`").
- `server/api/deploy.py:19` → `os.environ.get("CCSWITCH_DEPLOY_SOCKET", hostpaths.DEPLOY_SOCKET)` with `from server import hostpaths`.
- `server/sessions/deploy_snapshot.py:5` → `from server.hostpaths import DEPLOY_SNAPSHOT_PATH as DEFAULT_PATH`.
- `server/sessions/daemon_health.py:36` → `from server.hostpaths import HEALTH_STATE_DIR as _DEFAULT_HEALTH_STATE_DIR` (keep the "Mirror the watchdog's default" comment).
- `server/config.py:199` and `:492` → replace the literal `"/var/log/ccswitch"` with `hostpaths.LOG_DIR` (`from server import hostpaths` at the top; both the pydantic and the dataclass fallback copies).
- `server/sessions/push.py:225` → `print(f"# Paste into {hostpaths.ENV_FILE}, then restart the services:")`.

Do **not** touch `~/.cache/ccswitch`, `~/.local/lib/ccswitch`, `ccswitch.db`, `ccswitch.jsonl`, or the `.ccswitch/` repo namespace; those are user-host and namespace paths handled in Phases 3 and 4.

- [ ] **Step 4: Run the new test and the suites that pin these paths**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_hostpaths.py server/tests/test_ipc_endpoint.py server/tests/test_endpoint_roundtrip.py server/tests/test_deploy_cli.py server/tests/test_daemon_health_monitor.py server/tests/test_fleet_bootstrap.py -v --timeout=120`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add server/hostpaths.py server/ipc/paths.py server/deploy/serve.py server/api/deploy.py server/sessions/deploy_snapshot.py server/sessions/daemon_health.py server/config.py server/sessions/push.py server/tests/test_hostpaths.py
printf 'refactor(hostpaths): one module for /etc /run /var/lib /var/log stems\n\nFour modules each spelled the host paths; Phase 3 renames them per host, so they\nnow derive from brand.HOST_SLUG in server/hostpaths.py.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c3.txt
git commit -F /tmp/c3.txt
```

---

### Task 4: Python display and outbound-identity strings through `brand`

**Files:**
- Modify: `server/app.py:620`
- Modify: `server/ipc/daemon.py:29`
- Modify: `server/codex/protocol.py:92`
- Modify: `src/ccswitch/oauth.py:23`
- Modify: `server/github/app_auth.py:81`
- Modify: `server/api/snapshots.py:60`
- Modify: `server/automations/context.py:104`
- Modify: `server/sessions/orchestrator.py:4041,7231`
- Modify: `server/obs/metrics.py:253`
- Modify: `scripts/run_evals.py:65`

**Interfaces:**
- Consumes: `brand.NAME`, `brand.SLUG` (Task 1).
- Produces: nothing new; every string keeps its current value.

- [ ] **Step 1: Confirm the current pins still pass (baseline)**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_oauth.py::TestUserAgent server/tests/test_codex_schema.py -v --timeout=120`
Expected: PASS. (`tests/test_oauth.py` asserts the UA starts with `ccswitch/`; it stays true until Task 7.)

- [ ] **Step 2: Route each string**

Add `from ccswitch import brand` to each module, then:

- `server/app.py:620` → `FastAPI(title=brand.NAME, version="0.1.0", lifespan=_lifespan)`
- `server/ipc/daemon.py:29` → `FastAPI(title=f"{brand.SLUG}-sessiond")`
- `server/codex/protocol.py:92` → `name=brand.SLUG, version="1", title=f"{brand.NAME} session manager"`
- `src/ccswitch/oauth.py:23` → `USER_AGENT = f"{brand.SLUG}/{_VERSION}"` (`from . import brand`)
- `server/github/app_auth.py:81` → `"user-agent": brand.SLUG,`
- `server/api/snapshots.py:60` → `f'attachment; filename="{brand.SLUG}-config-bundle.tar.gz"'`
- `server/automations/context.py:104` → `f"{brand.NAME} — {self.automation_id}", summary,`
- `server/sessions/orchestrator.py:4041` → `"needs_input", f"{brand.NAME} — needs you", f"{name}: {status}",`
- `server/sessions/orchestrator.py:7231` → `"pr_broke", f"{brand.NAME} — PR broke", f"{rec.name}: {repo} CI failing",`
- `server/obs/metrics.py:253` → `f"Codex threads registered to a live {brand.NAME} session",`
- `scripts/run_evals.py:65` → `argparse.ArgumentParser(description=f"Run the {brand.NAME} eval suite.")`

- [ ] **Step 3: Verify no display literal remains**

Run: `git grep -nE 'title="ccswitch|"user-agent": "ccswitch|ccswitch-config-bundle|"ccswitch — |f"ccswitch — |ccswitch/\{_VERSION' -- server src scripts ':!*/tests/*'`
Expected: no output.

- [ ] **Step 4: Run the touched suites**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_oauth.py server/tests/test_codex_schema.py server/tests/test_snapshots.py server/tests/test_population_app_token.py -v --timeout=120`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/app.py server/ipc/daemon.py server/codex/protocol.py src/ccswitch/oauth.py server/github/app_auth.py server/api/snapshots.py server/automations/context.py server/sessions/orchestrator.py server/obs/metrics.py scripts/run_evals.py
printf 'refactor(brand): route display strings and User-Agents through ccswitch.brand\n\nNo string changes; every product-name mention in server/ src/ scripts/ now derives\nfrom brand.NAME / brand.SLUG.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c4.txt
git commit -F /tmp/c4.txt
```

---

### Task 5: CLI message prefixes through `brand.CLI_NAME`

**Files:**
- Modify: `src/ccswitch/cli.py` (lines 57, 70, 99–176, 215–276, 293–320, 325, 374, 385, 425–439)
- Modify: `src/ccswitch/assistant_mcp.py:30,85,88,96`

**Interfaces:**
- Consumes: `brand.CLI_NAME` (Task 1).

- [ ] **Step 1: Baseline the CLI tests**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_cli*.py -v`
Expected: PASS (record the count).

- [ ] **Step 2: Replace the literal prefix**

In `src/ccswitch/cli.py` add `from . import brand` and `_P = brand.CLI_NAME` near the imports, then change each `"ccswitch …"` message prefix to an f-string on `_P`. Examples of the exact shape for every site:

```python
print(f"{_P}: {exc}", file=sys.stderr)                                 # line 57
print(f"{_P} accounts: unknown sub-command {args.accounts_cmd!r}", file=sys.stderr)  # 70
print(f"{_P} crypto: no master key configured — encryption DISABLED")  # 99
print(f"{_P} crypto: run `{_P} crypto migrate` to encrypt existing slots.")  # 134
print(f"{_P} clones doctor: no session worktrees under {repo_root}")   # 215
print(f"{_P} reauth: account {args.name!r} recovered — status ok.")   # 320
parser = argparse.ArgumentParser(prog=_P)                              # 325
help=f"{_P} edge base URL (default: {assistant_mcp.DEFAULT_URL})"      # 374 (and 385)
print(f"export CLAUDE_CONFIG_DIR={shlex.quote(str(cfg_dir))}  # {_P}: {name}")  # 439
```

Apply the same to `src/ccswitch/assistant_mcp.py` lines 30, 85, 88, 96 (`f"{_P} mcp: …"`). The module docstring on `cli.py:1` and `worktrees_janitor.py:284`'s recovery hint (`ccswitch clones repair`) also become `{_P}`-based; `launcher.py:100`'s `prog="ccmax"` and the `ccrun:`/`ccmax:` prefixes stay (those binaries are not renamed).

- [ ] **Step 3: Verify**

Run: `git grep -nE '"ccswitch[ :]|f"ccswitch[ :]' -- src/ccswitch/cli.py src/ccswitch/assistant_mcp.py`
Expected: no output.
Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_cli*.py tests/test_assistant_mcp*.py -v`
Expected: same count as Step 1, all PASS.

- [ ] **Step 4: Commit**

```bash
git add src/ccswitch/cli.py src/ccswitch/assistant_mcp.py src/ccswitch/worktrees_janitor.py
printf 'refactor(cli): message prefixes derive from brand.CLI_NAME\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c5.txt
git commit -F /tmp/c5.txt
```

---

### Task 6: `web/src/brand.ts` and the web display strings

**Files:**
- Create: `web/src/brand.ts`
- Modify: `web/src/shell/documentTitle.ts:21`
- Modify: `web/src/shell/regions/TopBar.tsx:30`
- Modify: `web/src/components/DeployOverlay.tsx:69`
- Modify: `web/src/components/AdoptPanel.tsx:12,36`
- Modify: `web/src/shell/regions/HelpSheet.tsx:65`
- Modify: `web/src/shell/navDefs.ts:101`
- Modify: `web/src/session/appActions.ts:9`

**Interfaces:**
- Produces: `BRAND_NAME: string` and `MCP_TOOL_PREFIX: string` from `@/brand`. `APP_NAME` in `documentTitle.ts` stays exported (11 consumers) but equals `BRAND_NAME`.

- [ ] **Step 1: Baseline**

Run: `(cd web && npm run test -- --run 2>&1 | tail -5)`
Expected: all files pass; note the count.

- [ ] **Step 2: Create the module**

```ts
// web/src/brand.ts
/**
 * Brand identity — the ONE place the product name is spelled in the web app.
 * Mirrors `src/ccswitch/brand.py`; the two must agree (spec
 * docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md §1).
 */

/** Display name: top bar, document titles, overlays, help copy. */
export const BRAND_NAME = 'ccswitch'

/**
 * Wire prefix of the app's own MCP tools (`mcp__<server>__<tool>`). A contract
 * with the server's `brand.MCP_SERVER_NAME`; flips in rebrand Phase 2 with a
 * dual-accept window — never edit it on its own.
 */
export const MCP_TOOL_PREFIX = 'mcp__ccswitch__'
```

- [ ] **Step 3: Route the consumers**

- `documentTitle.ts:21` → `import { BRAND_NAME } from '@/brand'` and `export const APP_NAME = BRAND_NAME`.
- `TopBar.tsx:30` → `<span className="text-base font-semibold tracking-tight">{BRAND_NAME}</span>` (import from `@/brand`).
- `DeployOverlay.tsx:69` → `<h2 …>Upgrading {BRAND_NAME}</h2>`.
- `AdoptPanel.tsx:12` → `` tracked: `Already managed by ${BRAND_NAME}.`, `` and `:36` → `{BRAND_NAME} will {active ? 'stop' : 'resume'} the …`.
- `HelpSheet.tsx:65` → `` `Each account is an isolated CLAUDE_CONFIG_DIR; ${BRAND_NAME} rotates them as usage allows.`, ``
- `navDefs.ts:101` → `` help: `The product registry: the repos ${BRAND_NAME} tracks, their onboarding and their dependency edges. Triage review is its own section.` ``
- `appActions.ts:9` → delete the local `MCP_CCSWITCH_PREFIX` constant, `import { MCP_TOOL_PREFIX } from '@/brand'`, and rename its two uses at lines 99–100.

Leave `web/index.html`, `web/public/sw.js`, `favicon.svg`, and `BrandMark.tsx` for Task 7 (static files cannot import; the SVG gradient id is Phase 5b).

- [ ] **Step 4: Verify (full web gate — shared components are mocked in ~47 test files)**

Run: `(cd web && npm run typecheck && npm run lint && npm run test -- --run 2>&1 | tail -5)`
Expected: same pass count as Step 1; lint clean with `--max-warnings 0`.

- [ ] **Step 5: Commit**

```bash
git add web/src/brand.ts web/src/shell/documentTitle.ts web/src/shell/regions/TopBar.tsx web/src/components/DeployOverlay.tsx web/src/components/AdoptPanel.tsx web/src/shell/regions/HelpSheet.tsx web/src/shell/navDefs.ts web/src/session/appActions.ts
printf 'refactor(web): brand.ts owns the product name and the MCP tool prefix\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c6.txt
git commit -F /tmp/c6.txt
```

**PR A close-out:** run the full four-step Python gate and the full web gate (Global Constraints), `rm -rf web/dist`, check in-flight PRs by file (`server/assistant/tools.py`, `server/sessions/permissions.py`, `server/config.py`, `web/src/shell/documentTitle.ts`) with the GraphQL query from `.agents/instructions/core.md`, push, open the PR with this plan file included, `gh pr merge --squash --auto`.

---

## PR B — Phase 1a + 1b: the flip

Branch: `rebrand/phase-1-viberpit-display` off `origin/master` **after PR A merges**.

### Task 7: Flip the display name to ViberPit

**Files:**
- Modify: `src/ccswitch/brand.py` (`NAME`, `SLUG`)
- Modify: `web/src/brand.ts` (`BRAND_NAME`)
- Modify: `web/index.html:7,22`
- Modify: `web/public/sw.js:1,5,10`
- Modify: `web/public/favicon.svg:2`, `web/src/components/BrandMark.tsx:1-3` (comments only; gradient id unchanged)
- Modify tests: `tests/test_oauth.py:215`, `web/src/shell/documentTitle.test.tsx:18,25,30,37,45,59,68,77,82,98`, `web/src/shell/regions/PageHost.test.tsx:440,446`, `web/src/features/settings/SettingsPage.test.tsx:102`, `web/src/features/settings/pageContract.test.tsx:87`, `web/src/App.test.tsx:126,193`, and any line `grep -rn "ccswitch" web/src --include='*.test.*'` reports that pins a *display* string (`Upgrading ccswitch`, ` — ccswitch`).

**Interfaces:**
- Consumes: everything routed in Tasks 1–6.

- [ ] **Step 1: Update the tests first (they define the new contract)**

`tests/test_oauth.py` — the `TestUserAgent` assertion becomes:
```python
        self.assertTrue(
            captured["ua"].startswith("viberpit/"),
```
Web: replace every ` — ccswitch` with ` — ViberPit`, `'ccswitch'` document-title / `getByText` expectations with `'ViberPit'`, `(2) ccswitch — needs you` with `(2) ViberPit — needs you`, `Upgrading ccswitch` with `Upgrading ViberPit`. Do **not** touch tests that pin `ccswitch.` localStorage keys, `mcp__ccswitch__`, `steiner385/ccswitch`, `/etc/ccswitch`, or `ccswitch/` branch names.

- [ ] **Step 2: Run to verify they fail**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest tests/test_oauth.py::TestUserAgent -v && (cd web && npm run test -- --run src/shell/documentTitle.test.tsx 2>&1 | tail -3)`
Expected: both FAIL on the old name.

- [ ] **Step 3: Flip the constants and the static files**

`src/ccswitch/brand.py`:
```python
NAME = "ViberPit"
SLUG = "viberpit"
```
`web/src/brand.ts`: `export const BRAND_NAME = 'ViberPit'`.
`web/index.html:7` → `content="ViberPit — multi-account AI coding-agent session manager"`; `:22` → `<title>ViberPit</title>`.
`web/public/sw.js:1` comment → `ViberPit Web Push service worker`; `:5` → `data.title || 'ViberPit'`; `:10` → `tag: data.tag || 'viberpit'`.
`favicon.svg:2` and `BrandMark.tsx:1-3` comments → "ViberPit mark …" / `"ViberPit" text`. Keep the glyph (spec D3) and the `ccswitch-brand-grad` id (Phase 5b).

- [ ] **Step 4: Run the full gates**

Run: the four-step Python gate with `PYTHONPATH=$PWD/src` and all `CCSWITCH_*` unset (server suite in the background, tee to `/tmp/rebrand-p1-server.log`), then `(cd web && npm run typecheck && npm run lint && npm run test -- --run && npm run build)`, then `rm -rf web/dist`.
Expected: all green. If a server test fails on a display string, it is a pin this plan missed: update the expectation to the `ViberPit` form and list it in the PR body.

- [ ] **Step 5: Grep gate**

Run: `git grep -niE '"ccswitch"|ccswitch — |>ccswitch<|Upgrading ccswitch|by ccswitch' -- server src web/src web/index.html web/public scripts ':!*/tests/*' ':!*.test.*'`
Expected: only `src/ccswitch/brand.py` (`LEGACY_SLUG = "ccswitch"`) and `web/src/brand.ts` (`MCP_TOOL_PREFIX`).

- [ ] **Step 6: Commit**

```bash
git add -A src/ccswitch/brand.py web/src/brand.ts web/index.html web/public/sw.js web/public/favicon.svg web/src/components/BrandMark.tsx tests/test_oauth.py web/src
printf 'feat(brand): the product is now ViberPit on every display surface\n\nWeb title/top bar/overlays/help, FastAPI titles, both User-Agents, push titles,\nthe config-bundle filename, and the Codex client identity read ViberPit/viberpit.\nWire contracts (MCP prefix, host paths, cookie, markers, env names, CLI binary)\nare unchanged by design; see the spec for their phases.\n\nRefs docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md §3 Phase 1a.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c7.txt
git commit -F /tmp/c7.txt
```

---

### Task 8: Text posted to GitHub (Phase 1b)

**Files:**
- Modify: `server/automations/backlog_github.py:569,869,884,936,964,1073`
- Modify: `server/sessions/orchestrator.py:4325`
- Modify: `server/github/ci_issues.py:19`
- Modify: `server/automations/cicd/findings.py:13`
- Modify: `src/ccswitch/worktrees_janitor.py:136-139`
- Test: `server/tests/test_brand_github_text.py`

**Interfaces:**
- Consumes: `brand.NAME`, `brand.SLUG`; seams `backlog_github._escalate_issue(ref, reason, pr_link, *, gh_run)`, `backlog_github._release_tracking_issue(ref, open_children, pr_link, *, gh_run)`, `backlog_github.ensure_claim_labels(repos, *, gh_run)`, `ci_issues._ensure_label(repo, run)`, `findings.issue_body(title, detail)`. `gh_run`/`run` fakes take `(args, token=None)` and return an object with `returncode`.

Pre-req verified during planning: `heal_orphan_claims` detects orphans from the `in-progress` label and `find_referencing_pr`, not from the comment text, so changing the comment wording cannot strand old claims (spec §4 risk 8).

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_brand_github_text.py
"""Everything the automations post to GitHub names the product via brand.NAME.
These strings are public (issue comments, label descriptions), so the flip is
asserted here rather than left to a grep."""
from types import SimpleNamespace

from ccswitch import brand
from server.automations import backlog_github
from server.automations.cicd import findings
from server.github import ci_issues


def _capture():
    calls: list[list[str]] = []

    def run(args, token=None):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="")
    return calls, run


def _bodies(calls):
    return [args[args.index("--body") + 1] for args in calls if "--body" in args]


def test_escalation_comment_names_the_product():
    calls, run = _capture()
    backlog_github._escalate_issue("o/r#1", "docs-only diff", "https://pr/1", gh_run=run)
    assert any(f"by {brand.NAME} Backlog Runner" in b for b in _bodies(calls))
    assert not any("ccswitch" in b for b in _bodies(calls))


def test_release_comment_names_the_product():
    calls, run = _capture()
    backlog_github._release_tracking_issue("o/r#2", 3, "https://pr/2", gh_run=run)
    assert any(f"{brand.NAME} Backlog Runner left it open" in b for b in _bodies(calls))


def test_label_descriptions_name_the_product():
    calls, run = _capture()
    backlog_github.ensure_claim_labels(["o/r"], gh_run=run)
    descs = [args[args.index("--description") + 1] for args in calls]
    assert descs and all(d == f"{brand.SLUG} backlog-runner" for d in descs)

    calls2, run2 = _capture()
    ci_issues._ensure_label("o/r", run2)
    assert any(f"description=Filed by the {brand.NAME} CI/CD Doctor" in a
               for args in calls2 for a in args)


def test_cicd_issue_body_names_the_product():
    body = findings.issue_body("flaky", {"x": 1})
    assert body.endswith(f"Filed by the {brand.NAME} CI/CD Doctor (propose mode).")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_brand_github_text.py -v`
Expected: 4 FAIL (bodies still say `ccswitch`). (`ensure_claim_labels` calls `gitrepos.app_token_for_repo`; if that reaches the network in your environment, monkeypatch it to return `None` inside the test.)

- [ ] **Step 3: Route the strings**

Add `from ccswitch import brand` where missing, then:

- `backlog_github.py:569` → `"--description", f"{brand.SLUG} backlog-runner", "--force"],`
- `:869` → `f"{reason}. Escalated to a human (`needs-human`) by {brand.NAME} Backlog "`
- `:884` → `f"sub-issue(s), so {brand.NAME} Backlog Runner left it open (claim released). "`
- `:936` → `f"closed by {brand.NAME} Backlog Runner.")`
- `:964` → `f"returned to the backlog pool (left open) by {brand.NAME} Backlog Runner.",`
- `:1073` → `f"{brand.NAME} Backlog Runner (orphaned-claim heal).")`
- `orchestrator.py:4325` → `comment = (f"Claimed by {brand.NAME} Backlog Runner ({batch.primary}) — "`
- `ci_issues.py:19` → `"-f", f"description=Filed by the {brand.NAME} CI/CD Doctor"])`
- `findings.py:13` → `f"Filed by the {brand.NAME} CI/CD Doctor (propose mode).")` (make the whole return one f-string)
- `worktrees_janitor.py:136-139` → `f"user.email={brand.SLUG}-janitor@local"`, `f"user.name={brand.SLUG}-janitor"`, `f"{brand.SLUG}-janitor: max-age eviction"` (`from . import brand`).

- [ ] **Step 4: Run the new test and the neighbouring suites**

Run: `PYTHONPATH=$PWD/src /home/tony/GitHub/ccswitch/.venv/bin/python -m pytest server/tests/test_brand_github_text.py server/tests/test_backlog_runner_e2e.py server/tests/test_cicd_issues.py server/tests/test_cicd_file_issue_now.py server/tests/test_worktree_janitor.py -v --timeout=180`
Expected: all PASS (`test_backlog_runner_e2e.py:615` asserts only `"Backlog Runner" in body`).

- [ ] **Step 5: Commit, then PR B close-out**

```bash
git add server/automations/backlog_github.py server/sessions/orchestrator.py server/github/ci_issues.py server/automations/cicd/findings.py src/ccswitch/worktrees_janitor.py server/tests/test_brand_github_text.py
printf 'feat(brand): text posted to GitHub names ViberPit\n\nBacklog Runner claim/close/escalate comments, claim-label descriptions, the CI/CD\nDoctor label and issue body, and the janitor stash identity derive from brand.NAME.\nOrphan-claim healing keys on the in-progress label, not the comment text, so old\nclaims are unaffected.\n\nRefs docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md §3 Phase 1b.\n\nCo-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>\n' > /tmp/c8.txt
git commit -F /tmp/c8.txt
```

Then: full four-step gate + full web gate, in-flight PR check by file (`server/automations/backlog_github.py`, `server/sessions/orchestrator.py`), push, PR (body: what flipped, what deliberately did not, the list of test pins updated), ready, `gh pr merge --squash --auto`.

---

## Self-review

- **Spec coverage:** §1 name forms → Tasks 1, 6, 7. §3 Phase 0a (constants, MCP consolidation, path consolidation) → Tasks 1–3; display routing → Tasks 4–6. Phase 1a → Task 7 (CLI prefixes deliberately deferred, stated in Global Constraints; the About-section `/etc/ccswitch` path is a host path → Phase 3). Phase 1b → Task 8, including the risk-8 verification. Phase 0b deferred with reason. Phase 1c is out of scope by spec (pairs with 2a).
- **Placeholders:** none; every edit lists the file, line, and replacement text.
- **Type consistency:** `brand.NAME/SLUG/LEGACY_SLUG/CLI_NAME/HOST_SLUG/MCP_SERVER_NAME/MCP_TOOL_PREFIX` used identically in Tasks 2–8; `hostpaths` names in Task 3 match its test; web `BRAND_NAME`/`MCP_TOOL_PREFIX` match between Task 6 and Task 7.
