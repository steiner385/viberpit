# Library UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new top-level "Library" tab in the ccswitch web UI to view and manage Claude Code plugins (enable/disable), user skills and slash commands (full CRUD), and MCP servers (full CRUD), all against the shared config files ccswitch already mirrors across accounts.

**Architecture:** New `server/library/` package (pure filesystem scan/mutate functions over `ccswitch.paths.shared_base()` and `store/claude.shared.json`) + thin FastAPI router `server/api/library.py`. Frontend: new `library` tab, `web/src/components/library/` components, react-query hooks. No orchestrator/daemon involvement — direct atomic file IO.

**Tech Stack:** Python 3 / FastAPI / PyYAML (already a dep) · React + TS + Tailwind + @tanstack/react-query · pytest + vitest.

**Spec:** `docs/superpowers/specs/2026-07-12-library-ui-design.md` — read it first.

## Global Constraints

- Branch: work on `feat/library-ui` (already created off `origin/master`). Never commit to `master`.
- Python tests: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest <file> -v` from the repo root (no venv in this worktree — system python3).
- Web: run `npm ci` once in `web/` if `node_modules` is missing. Verify with `npm run test`, `npm run typecheck`, `npm run lint` (all in `web/`).
- mypy gate (CI-exact): `PYTHONPATH=.:src python3 -m mypy server/ --ignore-missing-imports` — new code must pass. If mypy is not installed locally, note it and rely on CI, but write typed code.
- **NEVER let a test touch the real `~/.claude`**: every test that exercises `server/library` MUST `monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "shared"))` FIRST (`ccswitch.paths.shared_base()` honors it — see `src/ccswitch/paths.py:17`). Read paths at call time in library code, never cache at module import.
- All JSON writes go through `ccswitch.creds.write_json_secure` (atomic tempfile + `os.replace`). Markdown writes use the `_write_text_atomic` helper defined in Task 3. Never write through an account cfg-dir symlink path — always the `shared_base()` real path.
- Mutation routes: validate identifiers via `server/library/names.py` (400 on failure), record an audit event via `server.audit.record_audit` (match the call pattern in `server/api/accounts.py`), and NEVER put MCP `config`/`headers`/`env` values in audit detail (may hold tokens) — only `{"name", "transport"}`.
- Error detail convention: `raise HTTPException(400, detail={"error": "invalid_name", "message": ...})` — match `server/api/accounts.py`.
- Frontend style: match existing components (`AccountsManager.tsx`, `SessionCard.tsx`, `AddAccountDialog.tsx`) for Tailwind classes, dialog structure (fixed overlay, Escape-to-close, `role="dialog" aria-modal`), and button idioms. Interactive controls need ≥44px touch targets on mobile per existing patterns.
- Commit after each task with a conventional-commit message ending in:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`

---

### Task 1: `server/library/names.py` — identifier validation

**Files:**
- Create: `server/library/__init__.py` (empty)
- Create: `server/library/names.py`
- Test: `server/tests/test_library_names.py`

**Interfaces:**
- Produces:
  - `valid_skill_name(name: str) -> bool` — lowercase slug, `^[a-z0-9][a-z0-9-]{0,63}$`
  - `valid_command_path(path: str) -> bool` — 1..4 `/`-joined segments, each `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, no segment equal to `.` or `..` or ending in `.md`
  - `valid_mcp_name(name: str) -> bool` — `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`
  - `valid_plugin_id(pid: str) -> bool` — `^[a-z0-9][a-z0-9._-]{0,63}@[a-z0-9][a-z0-9._-]{0,63}$`
  - `resolve_under(base: Path, *parts: str) -> Path | None` — joins, resolves, returns None unless strictly inside `base`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_library_names.py
"""Identifier validation for library entities — path-traversal defense."""
from pathlib import Path

from server.library.names import (
    resolve_under,
    valid_command_path,
    valid_mcp_name,
    valid_plugin_id,
    valid_skill_name,
)


def test_skill_names():
    assert valid_skill_name("my-skill")
    assert valid_skill_name("a")
    assert not valid_skill_name("My-Skill")     # uppercase
    assert not valid_skill_name("-lead")        # leading dash
    assert not valid_skill_name("has space")
    assert not valid_skill_name("")
    assert not valid_skill_name("a" * 65)
    assert not valid_skill_name("../etc")


def test_command_paths():
    assert valid_command_path("gh-fix")
    assert valid_command_path("project/gh-fix")
    assert valid_command_path("a/b/c/d")
    assert not valid_command_path("a/b/c/d/e")   # >4 segments
    assert not valid_command_path("gh-fix.md")   # extension is implicit
    assert not valid_command_path("../evil")
    assert not valid_command_path("a/../b")
    assert not valid_command_path("/abs")
    assert not valid_command_path("trail/")
    assert not valid_command_path("")


def test_mcp_and_plugin_ids():
    assert valid_mcp_name("github-issue-priority")
    assert valid_mcp_name("My.Server_1")
    assert not valid_mcp_name("has space")
    assert not valid_mcp_name("")
    assert valid_plugin_id("code-review@claude-plugins-official")
    assert not valid_plugin_id("code-review")            # no marketplace
    assert not valid_plugin_id("a@b@c")
    assert not valid_plugin_id("UPPER@market")


def test_resolve_under(tmp_path: Path):
    base = tmp_path / "base"
    base.mkdir()
    assert resolve_under(base, "x", "y.md") == (base / "x" / "y.md").resolve()
    assert resolve_under(base, "..", "escape") is None
    assert resolve_under(base, "x/../../escape") is None
    # symlink escape: base/link -> tmp_path
    (base / "link").symlink_to(tmp_path)
    assert resolve_under(base, "link", "f") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_library_names.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.library'`

- [ ] **Step 3: Write the implementation**

```python
# server/library/names.py
"""Identifier validation for library entities (path-traversal defense)."""
from __future__ import annotations

import re
from pathlib import Path

_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MCP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PLUGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}@[a-z0-9][a-z0-9._-]{0,63}$")


def valid_skill_name(name: str) -> bool:
    return bool(_SKILL_RE.match(name))


def valid_command_path(path: str) -> bool:
    if not path or path.startswith("/") or path.endswith("/"):
        return False
    segments = path.split("/")
    if not 1 <= len(segments) <= 4:
        return False
    for seg in segments:
        if seg in (".", "..") or seg.endswith(".md") or not _SEGMENT_RE.match(seg):
            return False
    return True


def valid_mcp_name(name: str) -> bool:
    return bool(_MCP_RE.match(name))


def valid_plugin_id(pid: str) -> bool:
    return bool(_PLUGIN_RE.match(pid))


def resolve_under(base: Path, *parts: str) -> Path | None:
    """Join parts under base and resolve; None unless the result stays inside base.

    Resolves symlinks, so a link pointing outside base is rejected too.
    """
    candidate = base.joinpath(*parts).resolve()
    base_resolved = base.resolve()
    if candidate == base_resolved:
        return None
    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        return None
    return candidate
```

Note on the symlink-escape test: `resolve()` follows the `base/link` symlink to `tmp_path/f`, which fails `relative_to(base)` — that is the behavior under test.

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_library_names.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add server/library/ server/tests/test_library_names.py
git commit -m "feat(library): identifier validation for library entities"
```

---

### Task 2: `server/library/scan.py` — read/list functions

**Files:**
- Create: `server/library/scan.py`
- Test: `server/tests/test_library_scan.py`

**Interfaces:**
- Consumes: `ccswitch.paths.shared_base()`, `ccswitch.paths.shared_claude_json_path()`, `ccswitch.paths.cfg_root()`, `server.library.names.resolve_under`
- Produces (all read the filesystem at call time; missing files → empty results, never raise):
  - `list_plugins() -> dict` — `{"plugins": [PluginSummary...], "marketplaces": [{"name","source","lastUpdated"}...]}` sorted by id. PluginSummary: `{"id","name","marketplace","enabled","version","installPath","installedAt","lastUpdated","description","category","components"}` where `components` is `{"commands": int, "skills": int, "agents": int, "mcpServers": int, "hooks": int}` (from catalog cache; zeros if absent).
  - `plugin_detail(plugin_id: str) -> dict | None` — summary plus `"commands": [{"path","description"}]`, `"skills": [{"name","description"}]`, `"agents": [str]`, `"mcpServers": [{"name","transport"}]`, `"hasHooks": bool` from a shallow scan of the newest installPath; None if the plugin is unknown.
  - `list_skills() -> dict` — `{"skills": [{"name","description","source","readonly","parseError"}...]}`; `source` is `"user"` or the plugin id; user skills first, then enabled plugins' skills, each group name-sorted.
  - `read_skill(name: str, plugin: str | None = None) -> dict | None` — `{"name","content","readonly"}`.
  - `list_commands() -> dict` — `{"commands": [{"path","name","description","source","readonly","parseError"}...]}`; `path` is the relative path without `.md` (e.g. `project/gh-fix`), `name` the final segment.
  - `read_command(path: str, plugin: str | None = None) -> dict | None` — `{"path","content","readonly"}`.
  - `list_mcp_servers() -> dict` — `{"servers": [{"name","source","readonly","transport","config","needsAuth"}...]}`; user servers (from `store/claude.shared.json → mcpServers`) then enabled plugins' servers; `needsAuth` is the sorted list of account names whose `mcp-needs-auth-cache.json` references the server.
  - Helpers other modules may reuse: `read_json_safe(path: Path) -> dict | None`, `parse_frontmatter(text: str) -> tuple[dict, bool]` (meta, parse_error), `enabled_plugin_paths() -> list[tuple[str, Path]]` (plugin_id, installPath for enabled plugins that have one), `installed_registry() -> dict` (parsed `installed_plugins.json`), `infer_transport(config: dict) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_library_scan.py
"""Library scan functions over a fabricated shared base + store."""
import json
from pathlib import Path

import pytest

from ccswitch import paths
from server.library import scan


@pytest.fixture()
def shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated shared base + ccswitch home. NEVER touch the real ~/.claude."""
    base = tmp_path / "shared"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "store").mkdir(parents=True)
    return base


def _seed_plugin(base: Path, name: str = "alpha", enabled: bool = True) -> Path:
    """Install one plugin with a command, a skill, and an MCP server."""
    pid = f"{name}@test-market"
    install = base / "plugins" / "cache" / "test-market" / name / "1.0.0"
    (install / "commands").mkdir(parents=True)
    (install / "commands" / "run.md").write_text("---\ndescription: Run it\n---\nbody\n")
    (install / "skills" / "helper").mkdir(parents=True)
    (install / "skills" / "helper" / "SKILL.md").write_text(
        "---\nname: helper\ndescription: Helps\n---\nbody\n"
    )
    (install / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"alpha-srv": {"command": "alpha-bin", "args": []}}}
    ))
    (base / "plugins").mkdir(exist_ok=True)
    (base / "plugins" / "installed_plugins.json").write_text(json.dumps({
        "version": 2,
        "plugins": {pid: [{
            "scope": "user", "installPath": str(install), "version": "1.0.0",
            "installedAt": "2026-01-01T00:00:00Z", "lastUpdated": "2026-06-01T00:00:00Z",
        }]},
    }))
    (base / "settings.json").write_text(json.dumps({"enabledPlugins": {pid: enabled}}))
    (base / "plugins" / "known_marketplaces.json").write_text(json.dumps(
        {"test-market": {"source": "github:test/market", "lastUpdated": "2026-06-01"}}
    ))
    return install


def test_empty_world_is_empty_not_error(shared: Path):
    assert scan.list_plugins() == {"plugins": [], "marketplaces": []}
    assert scan.list_skills() == {"skills": []}
    assert scan.list_commands() == {"commands": []}
    assert scan.list_mcp_servers() == {"servers": []}


def test_list_plugins_merges_registry_and_enabled(shared: Path):
    _seed_plugin(shared, "alpha", enabled=True)
    out = scan.list_plugins()
    assert len(out["plugins"]) == 1
    p = out["plugins"][0]
    assert p["id"] == "alpha@test-market"
    assert p["name"] == "alpha"
    assert p["marketplace"] == "test-market"
    assert p["enabled"] is True
    assert p["version"] == "1.0.0"
    assert out["marketplaces"] == [
        {"name": "test-market", "source": "github:test/market", "lastUpdated": "2026-06-01"}
    ]


def test_plugin_detail_scans_components(shared: Path):
    _seed_plugin(shared, "alpha")
    d = scan.plugin_detail("alpha@test-market")
    assert d is not None
    assert d["commands"] == [{"path": "run", "description": "Run it"}]
    assert d["skills"] == [{"name": "helper", "description": "Helps"}]
    assert d["mcpServers"] == [{"name": "alpha-srv", "transport": "stdio"}]
    assert scan.plugin_detail("nope@nowhere") is None


def test_list_skills_user_and_plugin(shared: Path):
    _seed_plugin(shared, "alpha")
    (shared / "skills" / "my-skill").mkdir(parents=True)
    (shared / "skills" / "my-skill" / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Mine\n---\nbody\n"
    )
    out = scan.list_skills()["skills"]
    assert [(s["name"], s["source"], s["readonly"]) for s in out] == [
        ("my-skill", "user", False),
        ("helper", "alpha@test-market", True),
    ]
    assert scan.read_skill("my-skill")["content"].endswith("body\n")
    assert scan.read_skill("helper", plugin="alpha@test-market")["readonly"] is True
    assert scan.read_skill("absent") is None


def test_malformed_frontmatter_flags_not_raises(shared: Path):
    (shared / "skills" / "broken").mkdir(parents=True)
    (shared / "skills" / "broken" / "SKILL.md").write_text("---\n: {invalid\n---\nx\n")
    out = scan.list_skills()["skills"]
    assert out[0]["name"] == "broken"
    assert out[0]["parseError"] is True


def test_list_commands_nested(shared: Path):
    (shared / "commands" / "project").mkdir(parents=True)
    (shared / "commands" / "top.md").write_text("---\ndescription: Top\n---\nx\n")
    (shared / "commands" / "project" / "nested.md").write_text("x\n")
    out = scan.list_commands()["commands"]
    assert [(c["path"], c["source"]) for c in out] == [
        ("project/nested", "user"), ("top", "user"),
    ]
    assert scan.read_command("project/nested")["content"] == "x\n"


def test_list_mcp_servers_user_plugin_and_auth(shared: Path, tmp_path: Path):
    _seed_plugin(shared, "alpha")
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.shared_claude_json_path(), {"mcpServers": {
        "mine": {"type": "http", "url": "https://x.example/mcp"},
    }})
    acct = paths.cfg_root() / "acct1"
    acct.mkdir(parents=True)
    (acct / "mcp-needs-auth-cache.json").write_text(json.dumps({
        "plugin:alpha:alpha-srv": {"timestamp": 1}, "mine": {"timestamp": 2},
    }))
    out = scan.list_mcp_servers()["servers"]
    by_name = {s["name"]: s for s in out}
    assert by_name["mine"]["source"] == "user"
    assert by_name["mine"]["transport"] == "http"
    assert by_name["mine"]["needsAuth"] == ["acct1"]
    assert by_name["alpha-srv"]["readonly"] is True
    assert by_name["alpha-srv"]["needsAuth"] == ["acct1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_library_scan.py -v`
Expected: FAIL — `ImportError: cannot import name 'scan'`

- [ ] **Step 3: Write the implementation**

```python
# server/library/scan.py
"""Read-side library functions: plugins, skills, commands, MCP servers.

All functions read the filesystem at call time (shared_base() honors
CLAUDE_CONFIG_DIR, which tests point at a temp dir). Missing or malformed
files degrade to empty results / parseError flags — listing never raises.
Never walks plugins/cache/ wholesale (it is tens of GB); only the shallow
component dirs of installed plugins' installPath.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from ccswitch import paths

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


def read_json_safe(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_frontmatter(text: str) -> tuple[dict[str, Any], bool]:
    """Return (meta, parse_error). No frontmatter → ({}, False)."""
    m = _FM_RE.match(text)
    if not m:
        return {}, False
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}, True
    return (data, False) if isinstance(data, dict) else ({}, True)


def installed_registry() -> dict[str, list[dict[str, Any]]]:
    reg = read_json_safe(paths.shared_base() / "plugins" / "installed_plugins.json") or {}
    plugins = reg.get("plugins")
    return plugins if isinstance(plugins, dict) else {}


def _enabled_map() -> dict[str, bool]:
    settings = read_json_safe(paths.shared_base() / "settings.json") or {}
    enabled = settings.get("enabledPlugins")
    return {k: bool(v) for k, v in enabled.items()} if isinstance(enabled, dict) else {}


def _newest_install(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return max(entries, key=lambda e: str(e.get("lastUpdated") or e.get("installedAt") or "")) if entries else {}


def _catalog() -> dict[str, Any]:
    cache = read_json_safe(paths.shared_base() / "plugins" / "plugin-catalog-cache.json") or {}
    plugins = (cache.get("catalog") or {}).get("plugins")
    return plugins if isinstance(plugins, dict) else {}


def enabled_plugin_paths() -> list[tuple[str, Path]]:
    """(plugin_id, installPath) for enabled plugins whose installPath exists."""
    enabled = _enabled_map()
    out: list[tuple[str, Path]] = []
    for pid, entries in sorted(installed_registry().items()):
        if not enabled.get(pid):
            continue
        install = _newest_install(entries).get("installPath")
        if install and Path(install).is_dir():
            out.append((pid, Path(install)))
    return out


def list_plugins() -> dict[str, Any]:
    enabled = _enabled_map()
    registry = installed_registry()
    catalog = _catalog()
    plugins: list[dict[str, Any]] = []
    for pid in sorted(set(registry) | set(enabled)):
        newest = _newest_install(registry.get(pid, []))
        cat = catalog.get(pid) or {}
        entry = cat.get("marketplace_entry") or {}
        components = cat.get("components") or {}
        name, _, marketplace = pid.partition("@")
        plugins.append({
            "id": pid,
            "name": name,
            "marketplace": marketplace,
            "enabled": enabled.get(pid, False),
            "version": newest.get("version") or cat.get("version"),
            "installPath": newest.get("installPath"),
            "installedAt": newest.get("installedAt"),
            "lastUpdated": newest.get("lastUpdated"),
            "description": entry.get("description"),
            "category": entry.get("category"),
            "components": {
                key: len(components.get(key) or [])
                for key in ("commands", "skills", "agents", "mcpServers", "hooks")
            },
        })
    markets = read_json_safe(paths.shared_base() / "plugins" / "known_marketplaces.json") or {}
    marketplaces = [
        {"name": name, "source": (m or {}).get("source"), "lastUpdated": (m or {}).get("lastUpdated")}
        for name, m in sorted(markets.items())
        if isinstance(m, dict) or m is None
    ]
    return {"plugins": plugins, "marketplaces": marketplaces}


def _plugin_mcp_servers(install: Path) -> dict[str, dict[str, Any]]:
    """Normalize a plugin .mcp.json: {'mcpServers': {...}} or flat {name: cfg}."""
    raw = read_json_safe(install / ".mcp.json") or {}
    inner = raw.get("mcpServers")
    if isinstance(inner, dict):
        raw = inner
    return {
        name: cfg for name, cfg in raw.items()
        if isinstance(cfg, dict) and ({"command", "url", "httpUrl", "type"} & set(cfg))
    }


def infer_transport(config: dict[str, Any]) -> str:
    t = config.get("type")
    if t in ("stdio", "http", "sse"):
        return str(t)
    if "command" in config:
        return "stdio"
    return "http"


def _scan_command_dir(root: Path, readonly: bool, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for md in sorted(root.rglob("*.md")):
        if not md.is_file():
            continue
        rel = md.relative_to(root).as_posix()[: -len(".md")]
        try:
            meta, err = parse_frontmatter(md.read_text())
        except OSError:
            meta, err = {}, True
        out.append({
            "path": rel,
            "name": rel.rsplit("/", 1)[-1],
            "description": meta.get("description"),
            "source": source,
            "readonly": readonly,
            "parseError": err,
        })
    return out


def _scan_skill_dir(root: Path, readonly: bool, source: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for skill_md in sorted(root.glob("*/SKILL.md")):
        try:
            meta, err = parse_frontmatter(skill_md.read_text())
        except OSError:
            meta, err = {}, True
        out.append({
            "name": skill_md.parent.name,
            "description": meta.get("description"),
            "source": source,
            "readonly": readonly,
            "parseError": err,
        })
    return out


def plugin_detail(plugin_id: str) -> dict[str, Any] | None:
    summary = next((p for p in list_plugins()["plugins"] if p["id"] == plugin_id), None)
    if summary is None:
        return None
    install = Path(summary["installPath"]) if summary.get("installPath") else None
    detail = dict(summary)
    if install and install.is_dir():
        detail["commands"] = [
            {"path": c["path"], "description": c["description"]}
            for c in _scan_command_dir(install / "commands", True, plugin_id)
        ]
        detail["skills"] = [
            {"name": s["name"], "description": s["description"]}
            for s in _scan_skill_dir(install / "skills", True, plugin_id)
        ]
        detail["agents"] = sorted(a.stem for a in (install / "agents").glob("*.md")) if (install / "agents").is_dir() else []
        detail["mcpServers"] = [
            {"name": name, "transport": infer_transport(cfg)}
            for name, cfg in sorted(_plugin_mcp_servers(install).items())
        ]
        detail["hasHooks"] = (install / "hooks").is_dir()
    else:
        detail.update({"commands": [], "skills": [], "agents": [], "mcpServers": [], "hasHooks": False})
    return detail


def list_skills() -> dict[str, Any]:
    skills = _scan_skill_dir(paths.shared_base() / "skills", False, "user")
    for pid, install in enabled_plugin_paths():
        skills.extend(_scan_skill_dir(install / "skills", True, pid))
    return {"skills": skills}


def read_skill(name: str, plugin: str | None = None) -> dict[str, Any] | None:
    if plugin is not None:
        install = dict(enabled_plugin_paths()).get(plugin)
        root = install / "skills" if install else None
    else:
        root = paths.shared_base() / "skills"
    if root is None:
        return None
    path = root / name / "SKILL.md"
    if not path.is_file():
        return None
    return {"name": name, "content": path.read_text(), "readonly": plugin is not None}


def list_commands() -> dict[str, Any]:
    commands = _scan_command_dir(paths.shared_base() / "commands", False, "user")
    for pid, install in enabled_plugin_paths():
        commands.extend(_scan_command_dir(install / "commands", True, pid))
    return {"commands": commands}


def read_command(path: str, plugin: str | None = None) -> dict[str, Any] | None:
    if plugin is not None:
        install = dict(enabled_plugin_paths()).get(plugin)
        root = install / "commands" if install else None
    else:
        root = paths.shared_base() / "commands"
    if root is None:
        return None
    file = root / f"{path}.md"
    if not file.is_file():
        return None
    return {"path": path, "content": file.read_text(), "readonly": plugin is not None}


def _needs_auth_by_server() -> dict[str, list[str]]:
    """server-name → sorted account names that flag it in mcp-needs-auth-cache.json.

    Cache keys are either the bare server name (user scope) or
    'plugin:<plugin-name>:<server-name>'.
    """
    out: dict[str, set[str]] = {}
    cfg_root = paths.cfg_root()
    if not cfg_root.is_dir():
        return {}
    for acct_dir in sorted(cfg_root.iterdir()):
        cache = read_json_safe(acct_dir / "mcp-needs-auth-cache.json") or {}
        for key in cache:
            server = key.rsplit(":", 1)[-1] if key.startswith("plugin:") else key
            out.setdefault(server, set()).add(acct_dir.name)
    return {name: sorted(accts) for name, accts in out.items()}


def list_mcp_servers() -> dict[str, Any]:
    needs_auth = _needs_auth_by_server()
    servers: list[dict[str, Any]] = []
    shared = read_json_safe(paths.shared_claude_json_path()) or {}
    user_servers = shared.get("mcpServers")
    if isinstance(user_servers, dict):
        for name, cfg in sorted(user_servers.items()):
            if not isinstance(cfg, dict):
                continue
            servers.append({
                "name": name, "source": "user", "readonly": False,
                "transport": infer_transport(cfg), "config": cfg,
                "needsAuth": needs_auth.get(name, []),
            })
    for pid, install in enabled_plugin_paths():
        for name, cfg in sorted(_plugin_mcp_servers(install).items()):
            servers.append({
                "name": name, "source": pid, "readonly": True,
                "transport": infer_transport(cfg), "config": cfg,
                "needsAuth": needs_auth.get(name, []),
            })
    return {"servers": servers}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_library_scan.py server/tests/test_library_names.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/library/scan.py server/tests/test_library_scan.py
git commit -m "feat(library): filesystem scan of plugins, skills, commands, MCP servers"
```

---

### Task 3: `server/library/mutate.py` — mutations

**Files:**
- Create: `server/library/mutate.py`
- Test: `server/tests/test_library_mutate.py`

**Interfaces:**
- Consumes: `ccswitch.creds.write_json_secure`, `ccswitch.paths`, `scan.read_json_safe`, `scan.installed_registry`
- Produces:
  - `set_plugin_enabled(plugin_id: str, enabled: bool) -> bool` — False if the plugin is in neither `enabledPlugins` nor the installed registry (unknown).
  - `write_skill(name: str, content: str) -> None`
  - `delete_skill(name: str) -> bool` — False if absent; removes the whole skill dir.
  - `write_command(path: str, content: str) -> None`
  - `delete_command(path: str) -> bool`
  - `put_mcp_server(name: str, config: dict) -> None`
  - `delete_mcp_server(name: str) -> bool`
  - `validate_mcp_config(config: dict) -> str | None` — error string or None. Rules: stdio requires non-empty `command: str`, optional `args: list[str]`, `env: dict[str,str]`; http/sse require exactly one of `url`/`httpUrl` (non-empty str), optional `headers: dict[str,str]`; extra keys are allowed (the binary supports more than we model).

**Callers pass only pre-validated identifiers** (the router validates via `names.py` before calling); mutate additionally guards with `resolve_under` as defense in depth and raises `ValueError` on failure.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_library_mutate.py
"""Library mutations — atomic writes into the shared base and store."""
import json
from pathlib import Path

import pytest

from ccswitch import paths
from server.library import mutate, scan


@pytest.fixture()
def shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "shared"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "store").mkdir(parents=True)
    return base


def test_set_plugin_enabled_roundtrip(shared: Path):
    (shared / "settings.json").write_text(json.dumps({
        "enabledPlugins": {"a@m": True},
        "permissions": {"allow": ["Bash"]},   # must survive untouched
    }))
    assert mutate.set_plugin_enabled("a@m", False) is True
    data = json.loads((shared / "settings.json").read_text())
    assert data["enabledPlugins"]["a@m"] is False
    assert data["permissions"] == {"allow": ["Bash"]}
    assert mutate.set_plugin_enabled("unknown@m", True) is False


def test_set_plugin_enabled_known_via_registry(shared: Path):
    (shared / "plugins").mkdir()
    (shared / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"b@m": [{"installPath": "/x", "version": "1"}]}}
    ))
    assert mutate.set_plugin_enabled("b@m", True) is True
    data = json.loads((shared / "settings.json").read_text())
    assert data["enabledPlugins"]["b@m"] is True


def test_skill_crud(shared: Path):
    mutate.write_skill("new-skill", "---\nname: new-skill\n---\nbody\n")
    assert scan.read_skill("new-skill")["content"].endswith("body\n")
    mutate.write_skill("new-skill", "updated\n")
    assert scan.read_skill("new-skill")["content"] == "updated\n"
    assert mutate.delete_skill("new-skill") is True
    assert scan.read_skill("new-skill") is None
    assert mutate.delete_skill("new-skill") is False


def test_command_crud_nested(shared: Path):
    mutate.write_command("project/deep", "cmd\n")
    assert scan.read_command("project/deep")["content"] == "cmd\n"
    assert mutate.delete_command("project/deep") is True
    assert mutate.delete_command("project/deep") is False


def test_traversal_rejected(shared: Path):
    with pytest.raises(ValueError):
        mutate.write_skill("../evil", "x")
    with pytest.raises(ValueError):
        mutate.write_command("../../etc/passwd", "x")


def test_mcp_crud(shared: Path):
    mutate.put_mcp_server("srv", {"type": "http", "url": "https://x/mcp"})
    data = json.loads(paths.shared_claude_json_path().read_text())
    assert data["mcpServers"]["srv"]["url"] == "https://x/mcp"
    mutate.put_mcp_server("srv", {"command": "bin", "args": ["-v"]})
    data = json.loads(paths.shared_claude_json_path().read_text())
    assert data["mcpServers"]["srv"] == {"command": "bin", "args": ["-v"]}
    assert mutate.delete_mcp_server("srv") is True
    assert mutate.delete_mcp_server("srv") is False


def test_mcp_preserves_other_shared_keys(shared: Path):
    from ccswitch.creds import write_json_secure
    write_json_secure(paths.shared_claude_json_path(), {"projects": {"/x": {}}})
    mutate.put_mcp_server("srv", {"command": "bin"})
    data = json.loads(paths.shared_claude_json_path().read_text())
    assert data["projects"] == {"/x": {}}


def test_validate_mcp_config():
    assert mutate.validate_mcp_config({"command": "bin"}) is None
    assert mutate.validate_mcp_config({"type": "http", "url": "https://x"}) is None
    assert mutate.validate_mcp_config({"type": "http"}) is not None      # no url
    assert mutate.validate_mcp_config({"type": "stdio"}) is not None     # no command
    assert mutate.validate_mcp_config({"command": ""}) is not None
    assert mutate.validate_mcp_config({"command": "b", "args": "notalist"}) is not None
    assert mutate.validate_mcp_config({"type": "sse", "url": "https://x", "headers": {"A": "b"}}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_library_mutate.py -v`
Expected: FAIL — `ImportError: cannot import name 'mutate'`

- [ ] **Step 3: Write the implementation**

```python
# server/library/mutate.py
"""Write-side library functions. All JSON writes are atomic via
write_json_secure; markdown writes are atomic tempfile+rename. Callers
(the router) pre-validate identifiers; resolve_under re-guards here as
defense in depth (ValueError on violation)."""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ccswitch import paths
from ccswitch.creds import write_json_secure

from .names import resolve_under
from .scan import installed_registry, read_json_safe


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-lib-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def set_plugin_enabled(plugin_id: str, enabled: bool) -> bool:
    settings_path = paths.shared_base() / "settings.json"
    data = read_json_safe(settings_path) or {}
    known = data.get("enabledPlugins") or {}
    if plugin_id not in known and plugin_id not in installed_registry():
        return False
    data.setdefault("enabledPlugins", {})[plugin_id] = enabled
    write_json_secure(settings_path, data)
    return True


def _skill_dir(name: str) -> Path:
    base = paths.shared_base() / "skills"
    base.mkdir(parents=True, exist_ok=True)
    resolved = resolve_under(base, name)
    if resolved is None:
        raise ValueError(f"invalid skill name: {name!r}")
    return resolved


def write_skill(name: str, content: str) -> None:
    _write_text_atomic(_skill_dir(name) / "SKILL.md", content)


def delete_skill(name: str) -> bool:
    d = _skill_dir(name)
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


def _command_file(path: str) -> Path:
    base = paths.shared_base() / "commands"
    base.mkdir(parents=True, exist_ok=True)
    resolved = resolve_under(base, *path.split("/"))
    if resolved is None:
        raise ValueError(f"invalid command path: {path!r}")
    return resolved.with_name(resolved.name + ".md")


def write_command(path: str, content: str) -> None:
    _write_text_atomic(_command_file(path), content)


def delete_command(path: str) -> bool:
    f = _command_file(path)
    if not f.is_file():
        return False
    f.unlink()
    return True


def validate_mcp_config(config: dict[str, Any]) -> str | None:
    transport = config.get("type") or ("stdio" if "command" in config else "http")
    if transport == "stdio":
        cmd = config.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return "stdio server requires a non-empty 'command'"
        if "args" in config and not (
            isinstance(config["args"], list) and all(isinstance(a, str) for a in config["args"])
        ):
            return "'args' must be a list of strings"
        if "env" in config and not (
            isinstance(config["env"], dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in config["env"].items())
        ):
            return "'env' must be a string→string map"
        return None
    if transport in ("http", "sse"):
        url = config.get("url") or config.get("httpUrl")
        if not isinstance(url, str) or not url.strip():
            return f"{transport} server requires a non-empty 'url'"
        if "headers" in config and not (
            isinstance(config["headers"], dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in config["headers"].items())
        ):
            return "'headers' must be a string→string map"
        return None
    return f"unknown transport type: {transport!r}"


def put_mcp_server(name: str, config: dict[str, Any]) -> None:
    shared_path = paths.shared_claude_json_path()
    data = read_json_safe(shared_path) or {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[name] = config
    data["mcpServers"] = servers
    write_json_secure(shared_path, data)


def delete_mcp_server(name: str) -> bool:
    shared_path = paths.shared_claude_json_path()
    data = read_json_safe(shared_path) or {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or name not in servers:
        return False
    del servers[name]
    write_json_secure(shared_path, data)
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_library_mutate.py server/tests/test_library_scan.py server/tests/test_library_names.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/library/mutate.py server/tests/test_library_mutate.py
git commit -m "feat(library): atomic mutations for plugin toggle, skills, commands, MCP servers"
```

---

### Task 4: `server/api/library.py` — router + registration

**Files:**
- Create: `server/api/library.py`
- Modify: `server/app.py` (import near line 37, `app.include_router(library_router)` near line 478)
- Test: `server/tests/test_library_api.py`

**Interfaces:**
- Consumes: `server.library.scan/mutate/names`, `server.auth.middleware.current_identity`, `server.db.get_db`, `server.audit.record_audit`
- Produces HTTP API (all under `/api/library`, all `Depends(current_identity)`):
  - `GET /plugins` → `scan.list_plugins()`
  - `GET /plugins/{plugin_id}` → detail or 404
  - `PATCH /plugins/{plugin_id}` body `{"enabled": bool}` → 400 invalid id, 404 unknown, else `{"id", "enabled"}`; audit `library_plugin_toggled`
  - `GET /skills` · `GET /skills/{name}?plugin=` · `PUT /skills/{name}` body `{"content": str}` → `{"name"}`; `DELETE /skills/{name}` → 204/404; audit `library_skill_saved`/`library_skill_deleted`
  - `GET /commands` · `GET /commands/{path:path}?plugin=` · `PUT /commands/{path:path}` · `DELETE /commands/{path:path}` — same shape; audit `library_command_saved`/`library_command_deleted`
  - `GET /mcp` · `PUT /mcp/{name}` body `{"config": dict}` (400 on `validate_mcp_config` failure) → `{"name", "transport"}`; `DELETE /mcp/{name}` → 204/404; audit `library_mcp_saved`/`library_mcp_deleted` with detail `{"name", "transport"}` ONLY (config may hold tokens)

Before writing, open `server/api/accounts.py` and copy its exact `record_audit` usage (db dependency + commit responsibility) — match it precisely.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_library_api.py
"""Library API routes over a fabricated shared base."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ccswitch import paths
from server.app import create_app

IDENTITY_HEADERS = {"Tailscale-User-Login": "libtest@example.com"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    base = tmp_path / "shared"
    base.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(base))
    monkeypatch.setenv("CCSWITCH_HOME", str(tmp_path / "home"))
    (tmp_path / "home" / "store").mkdir(parents=True)
    (base / "settings.json").write_text(json.dumps({"enabledPlugins": {"a@m": True}}))
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_plugins_list_and_toggle(client: TestClient):
    out = client.get("/api/library/plugins", headers=IDENTITY_HEADERS).json()
    assert out["plugins"][0]["id"] == "a@m"
    r = client.patch("/api/library/plugins/a@m", json={"enabled": False}, headers=IDENTITY_HEADERS)
    assert r.status_code == 200 and r.json() == {"id": "a@m", "enabled": False}
    out = client.get("/api/library/plugins", headers=IDENTITY_HEADERS).json()
    assert out["plugins"][0]["enabled"] is False
    assert client.patch("/api/library/plugins/nope@m", json={"enabled": True},
                        headers=IDENTITY_HEADERS).status_code == 404
    assert client.patch("/api/library/plugins/Bad Id!", json={"enabled": True},
                        headers=IDENTITY_HEADERS).status_code == 400


def test_skill_crud_roundtrip(client: TestClient):
    r = client.put("/api/library/skills/my-skill", json={"content": "hello\n"},
                   headers=IDENTITY_HEADERS)
    assert r.status_code == 200
    r = client.get("/api/library/skills/my-skill", headers=IDENTITY_HEADERS)
    assert r.json()["content"] == "hello\n"
    names = [s["name"] for s in client.get("/api/library/skills", headers=IDENTITY_HEADERS).json()["skills"]]
    assert "my-skill" in names
    assert client.delete("/api/library/skills/my-skill", headers=IDENTITY_HEADERS).status_code == 204
    assert client.get("/api/library/skills/my-skill", headers=IDENTITY_HEADERS).status_code == 404
    assert client.delete("/api/library/skills/my-skill", headers=IDENTITY_HEADERS).status_code == 404
    assert client.put("/api/library/skills/../evil", json={"content": "x"},
                      headers=IDENTITY_HEADERS).status_code in (400, 404)  # 404: path routing
    assert client.put("/api/library/skills/Bad Name", json={"content": "x"},
                      headers=IDENTITY_HEADERS).status_code == 400


def test_command_crud_nested(client: TestClient):
    r = client.put("/api/library/commands/project/deep", json={"content": "x\n"},
                   headers=IDENTITY_HEADERS)
    assert r.status_code == 200
    assert client.get("/api/library/commands/project/deep",
                      headers=IDENTITY_HEADERS).json()["content"] == "x\n"
    paths_ = [c["path"] for c in client.get("/api/library/commands", headers=IDENTITY_HEADERS).json()["commands"]]
    assert "project/deep" in paths_
    assert client.put("/api/library/commands/a/../b", json={"content": "x"},
                      headers=IDENTITY_HEADERS).status_code == 400
    assert client.delete("/api/library/commands/project/deep", headers=IDENTITY_HEADERS).status_code == 204


def test_mcp_crud(client: TestClient):
    r = client.put("/api/library/mcp/srv", json={"config": {"type": "http", "url": "https://x/mcp"}},
                   headers=IDENTITY_HEADERS)
    assert r.status_code == 200 and r.json() == {"name": "srv", "transport": "http"}
    servers = client.get("/api/library/mcp", headers=IDENTITY_HEADERS).json()["servers"]
    assert servers[0]["name"] == "srv" and servers[0]["source"] == "user"
    assert client.put("/api/library/mcp/bad", json={"config": {"type": "http"}},
                      headers=IDENTITY_HEADERS).status_code == 400
    assert client.delete("/api/library/mcp/srv", headers=IDENTITY_HEADERS).status_code == 204
    assert client.delete("/api/library/mcp/srv", headers=IDENTITY_HEADERS).status_code == 404
    # deletion persisted to the shared claude.json
    data = json.loads(paths.shared_claude_json_path().read_text())
    assert data.get("mcpServers", {}) == {}


def test_requires_identity(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    # conftest runs with an empty allowlist + CCSWITCH_DEV=1, which admits any
    # identity header. A request with no header at all must still 401.
    r = client.get("/api/library/plugins")
    assert r.status_code == 401
```

Note: if `test_requires_identity` turns out to pass identity through another
mechanism in dev mode (check what other API tests assert), mirror the
existing convention from e.g. `server/tests/test_add_account_api.py` instead
of inventing a new one — the point is parity with how other routers gate.

- [ ] **Step 2: Run test to verify it fails**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_library_api.py -v`
Expected: FAIL — 404s (router not registered)

- [ ] **Step 3: Write the implementation**

```python
# server/api/library.py
"""Library API — plugins, skills, slash commands, MCP servers over the
shared config base. Read routes are direct filesystem scans; mutations are
atomic writes + audit events. No orchestrator/daemon involvement."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.audit import record_audit
from server.auth.middleware import current_identity
from server.db import get_db
from server.library import mutate, scan
from server.library.names import (
    valid_command_path,
    valid_mcp_name,
    valid_plugin_id,
    valid_skill_name,
)

router = APIRouter(prefix="/api/library", tags=["library"])


class ToggleBody(BaseModel):
    enabled: bool


class ContentBody(BaseModel):
    content: str


class McpBody(BaseModel):
    config: dict


def _bad(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail={"error": "invalid_name", "message": message})


@router.get("/plugins")
async def get_plugins(_identity: str = Depends(current_identity)) -> dict:
    return scan.list_plugins()


@router.get("/plugins/{plugin_id}")
async def get_plugin(plugin_id: str, _identity: str = Depends(current_identity)) -> dict:
    if not valid_plugin_id(plugin_id):
        raise _bad("invalid plugin id")
    detail = scan.plugin_detail(plugin_id)
    if detail is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_plugin"})
    return detail


@router.patch("/plugins/{plugin_id}")
async def toggle_plugin(
    plugin_id: str, body: ToggleBody,
    identity: str = Depends(current_identity), db=Depends(get_db),
) -> dict:
    if not valid_plugin_id(plugin_id):
        raise _bad("invalid plugin id")
    if not mutate.set_plugin_enabled(plugin_id, body.enabled):
        raise HTTPException(status_code=404, detail={"error": "unknown_plugin"})
    await record_audit(db=db, type="library_plugin_toggled", identity=identity,
                       detail={"plugin": plugin_id, "enabled": body.enabled})
    return {"id": plugin_id, "enabled": body.enabled}


@router.get("/skills")
async def get_skills(_identity: str = Depends(current_identity)) -> dict:
    return scan.list_skills()


@router.get("/skills/{name}")
async def get_skill(name: str, plugin: str | None = None,
                    _identity: str = Depends(current_identity)) -> dict:
    if not valid_skill_name(name):
        raise _bad("invalid skill name")
    out = scan.read_skill(name, plugin=plugin)
    if out is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_skill"})
    return out


@router.put("/skills/{name}")
async def put_skill(name: str, body: ContentBody,
                    identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    if not valid_skill_name(name):
        raise _bad("invalid skill name")
    mutate.write_skill(name, body.content)
    await record_audit(db=db, type="library_skill_saved", identity=identity,
                       detail={"skill": name})
    return {"name": name}


@router.delete("/skills/{name}", status_code=204)
async def delete_skill(name: str,
                       identity: str = Depends(current_identity), db=Depends(get_db)) -> None:
    if not valid_skill_name(name):
        raise _bad("invalid skill name")
    if not mutate.delete_skill(name):
        raise HTTPException(status_code=404, detail={"error": "unknown_skill"})
    await record_audit(db=db, type="library_skill_deleted", identity=identity,
                       detail={"skill": name})


@router.get("/commands")
async def get_commands(_identity: str = Depends(current_identity)) -> dict:
    return scan.list_commands()


@router.get("/commands/{path:path}")
async def get_command(path: str, plugin: str | None = None,
                      _identity: str = Depends(current_identity)) -> dict:
    if not valid_command_path(path):
        raise _bad("invalid command path")
    out = scan.read_command(path, plugin=plugin)
    if out is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_command"})
    return out


@router.put("/commands/{path:path}")
async def put_command(path: str, body: ContentBody,
                      identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    if not valid_command_path(path):
        raise _bad("invalid command path")
    mutate.write_command(path, body.content)
    await record_audit(db=db, type="library_command_saved", identity=identity,
                       detail={"command": path})
    return {"path": path}


@router.delete("/commands/{path:path}", status_code=204)
async def delete_command(path: str,
                         identity: str = Depends(current_identity), db=Depends(get_db)) -> None:
    if not valid_command_path(path):
        raise _bad("invalid command path")
    if not mutate.delete_command(path):
        raise HTTPException(status_code=404, detail={"error": "unknown_command"})
    await record_audit(db=db, type="library_command_deleted", identity=identity,
                       detail={"command": path})


@router.get("/mcp")
async def get_mcp(_identity: str = Depends(current_identity)) -> dict:
    return scan.list_mcp_servers()


@router.put("/mcp/{name}")
async def put_mcp(name: str, body: McpBody,
                  identity: str = Depends(current_identity), db=Depends(get_db)) -> dict:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    error = mutate.validate_mcp_config(body.config)
    if error:
        raise HTTPException(status_code=400, detail={"error": "invalid_config", "message": error})
    mutate.put_mcp_server(name, body.config)
    transport = scan.infer_transport(body.config)
    # Audit name+transport only — config/headers/env may contain tokens.
    await record_audit(db=db, type="library_mcp_saved", identity=identity,
                       detail={"name": name, "transport": transport})
    return {"name": name, "transport": transport}


@router.delete("/mcp/{name}", status_code=204)
async def delete_mcp(name: str,
                     identity: str = Depends(current_identity), db=Depends(get_db)) -> None:
    if not valid_mcp_name(name):
        raise _bad("invalid server name")
    if not mutate.delete_mcp_server(name):
        raise HTTPException(status_code=404, detail={"error": "unknown_server"})
    await record_audit(db=db, type="library_mcp_deleted", identity=identity,
                       detail={"name": name})
```

Registration in `server/app.py` (match neighbors exactly):

```python
from server.api.library import router as library_router     # with the other imports ~line 37
...
    app.include_router(library_router)                        # after automations_router ~line 478
```

Check whether `record_audit` callers in `accounts.py` also `await db.commit()` — if they do, do the same; if the dependency commits, don't. Match exactly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/test_library_api.py -v`
Expected: PASS. Also run the three earlier library test files together.

- [ ] **Step 5: Check the request-layer breadth test**

Some repos have a breadth/parity test over routers (memory: "Request-layer orchestrator methods need a daemon proxy… or breadth tests fail" — that applies to orchestrator methods, which this router has none of). Run:
`env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/ -q -k "breadth or parity" --co -q` and run whatever collects. Expected: no failures caused by the new router.

- [ ] **Step 6: Commit**

```bash
git add server/api/library.py server/app.py server/tests/test_library_api.py
git commit -m "feat(library): /api/library router — plugins, skills, commands, MCP"
```

---

### Task 5: Frontend types + query hooks

**Files:**
- Modify: `web/src/api/types.ts` (append)
- Modify: `web/src/api/hooks.ts` (QK entries + hooks at the end of the file)
- Test: `web/src/api/libraryHooks.test.tsx`

**Interfaces:**
- Consumes: `/api/library/*` endpoints from Task 4 (client prefixes `/api`, so hook paths start `/library/...`)
- Produces (exact names later tasks import from `@/api/hooks` and `@/api/types`):
  - Types: `LibraryPlugin`, `LibraryPluginDetail`, `LibraryPluginsResponse`, `LibrarySkill`, `LibraryCommand`, `LibraryMcpServer`, `McpServerConfig`, `LibraryContent`
  - Hooks: `useLibraryPlugins()`, `useLibraryPluginDetail(id, enabled)`, `useTogglePlugin()`, `useLibrarySkills()`, `useLibraryItemContent(kind, id, plugin?)`, `useSaveLibraryItem(kind)`, `useDeleteLibraryItem(kind)`, `useLibraryMcp()`, `useSaveMcpServer()`, `useDeleteMcpServer()` — where `kind` is `'skills' | 'commands'`

- [ ] **Step 1: Append types to `web/src/api/types.ts`**

```ts
// --- Library (plugins / skills / commands / MCP) ---

export interface LibraryComponentCounts {
  commands: number
  skills: number
  agents: number
  mcpServers: number
  hooks: number
}

export interface LibraryPlugin {
  id: string
  name: string
  marketplace: string
  enabled: boolean
  version?: string | null
  installPath?: string | null
  installedAt?: string | null
  lastUpdated?: string | null
  description?: string | null
  category?: string | null
  components: LibraryComponentCounts
}

export interface LibraryPluginDetail extends LibraryPlugin {
  commands: { path: string; description?: string | null }[]
  skills: { name: string; description?: string | null }[]
  agents: string[]
  mcpServers: { name: string; transport: string }[]
  hasHooks: boolean
}

export interface LibraryMarketplace {
  name: string
  source?: string | null
  lastUpdated?: string | null
}

export interface LibraryPluginsResponse {
  plugins: LibraryPlugin[]
  marketplaces: LibraryMarketplace[]
}

export interface LibrarySkill {
  name: string
  description?: string | null
  source: string          // 'user' | plugin id
  readonly: boolean
  parseError?: boolean
}

export interface LibraryCommand {
  path: string
  name: string
  description?: string | null
  source: string
  readonly: boolean
  parseError?: boolean
}

export interface McpServerConfig {
  type?: string
  command?: string
  args?: string[]
  env?: Record<string, string>
  url?: string
  httpUrl?: string
  headers?: Record<string, string>
  [key: string]: unknown
}

export interface LibraryMcpServer {
  name: string
  source: string
  readonly: boolean
  transport: 'stdio' | 'http' | 'sse'
  config: McpServerConfig
  needsAuth: string[]
}

export interface LibraryContent {
  name?: string
  path?: string
  content: string
  readonly: boolean
}
```

- [ ] **Step 2: Add QK entries and hooks to `web/src/api/hooks.ts`**

Add to the `QK` object:

```ts
  libraryPlugins: ['library', 'plugins'] as const,
  libraryPlugin: (id: string) => ['library', 'plugin', id] as const,
  librarySkills: ['library', 'skills'] as const,
  libraryCommands: ['library', 'commands'] as const,
  libraryMcp: ['library', 'mcp'] as const,
  libraryContent: (kind: string, id: string, plugin?: string) =>
    ['library', 'content', kind, id, plugin ?? null] as const,
```

Append hooks (import the new types in the existing `types` import):

```ts
// --- Library (plugins / skills / commands / MCP) ---

export function useLibraryPlugins() {
  return useQuery<LibraryPluginsResponse>({
    queryKey: QK.libraryPlugins,
    queryFn: () => api.get<LibraryPluginsResponse>('/library/plugins'),
    staleTime: 30_000,
  })
}

export function useLibraryPluginDetail(id: string, enabled: boolean) {
  return useQuery<LibraryPluginDetail>({
    queryKey: QK.libraryPlugin(id),
    queryFn: () => api.get<LibraryPluginDetail>(`/library/plugins/${encodeURIComponent(id)}`),
    enabled,
    staleTime: 60_000,
  })
}

export function useTogglePlugin() {
  const qc = useQueryClient()
  return useMutation<{ id: string; enabled: boolean }, ApiRequestError, { id: string; enabled: boolean }>({
    mutationFn: ({ id, enabled }) =>
      api.patch(`/library/plugins/${encodeURIComponent(id)}`, { enabled }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.libraryPlugins })
      void qc.invalidateQueries({ queryKey: ['library', 'plugin'] })
      // plugin enablement changes which skills/commands/servers are visible
      void qc.invalidateQueries({ queryKey: QK.librarySkills })
      void qc.invalidateQueries({ queryKey: QK.libraryCommands })
      void qc.invalidateQueries({ queryKey: QK.libraryMcp })
    },
  })
}

export function useLibrarySkills() {
  return useQuery<{ skills: LibrarySkill[] }>({
    queryKey: QK.librarySkills,
    queryFn: () => api.get('/library/skills'),
    staleTime: 30_000,
  })
}

export function useLibraryCommands() {
  return useQuery<{ commands: LibraryCommand[] }>({
    queryKey: QK.libraryCommands,
    queryFn: () => api.get('/library/commands'),
    staleTime: 30_000,
  })
}

export type LibraryItemKind = 'skills' | 'commands'

export function useLibraryItemContent(kind: LibraryItemKind, id: string, plugin?: string) {
  const suffix = plugin ? `?plugin=${encodeURIComponent(plugin)}` : ''
  return useQuery<LibraryContent>({
    queryKey: QK.libraryContent(kind, id, plugin),
    queryFn: () => api.get<LibraryContent>(`/library/${kind}/${id}${suffix}`),
    enabled: id.length > 0,
  })
}

export function useSaveLibraryItem(kind: LibraryItemKind) {
  const qc = useQueryClient()
  return useMutation<unknown, ApiRequestError, { id: string; content: string }>({
    mutationFn: ({ id, content }) => api.put(`/library/${kind}/${id}`, { content }),
    onSuccess: (_data, { id }) => {
      void qc.invalidateQueries({ queryKey: kind === 'skills' ? QK.librarySkills : QK.libraryCommands })
      void qc.invalidateQueries({ queryKey: QK.libraryContent(kind, id) })
    },
  })
}

export function useDeleteLibraryItem(kind: LibraryItemKind) {
  const qc = useQueryClient()
  return useMutation<unknown, ApiRequestError, { id: string }>({
    mutationFn: ({ id }) => api.del(`/library/${kind}/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: kind === 'skills' ? QK.librarySkills : QK.libraryCommands })
    },
  })
}

export function useLibraryMcp() {
  return useQuery<{ servers: LibraryMcpServer[] }>({
    queryKey: QK.libraryMcp,
    queryFn: () => api.get('/library/mcp'),
    staleTime: 30_000,
  })
}

export function useSaveMcpServer() {
  const qc = useQueryClient()
  return useMutation<unknown, ApiRequestError, { name: string; config: McpServerConfig }>({
    mutationFn: ({ name, config }) =>
      api.put(`/library/mcp/${encodeURIComponent(name)}`, { config }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.libraryMcp }),
  })
}

export function useDeleteMcpServer() {
  const qc = useQueryClient()
  return useMutation<unknown, ApiRequestError, { name: string }>({
    mutationFn: ({ name }) => api.del(`/library/mcp/${encodeURIComponent(name)}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.libraryMcp }),
  })
}
```

(For command ids, path segments are pre-validated slugs — no per-segment encoding needed; do NOT `encodeURIComponent` the whole path or the `/` separators break.)

- [ ] **Step 3: Write a hook test (mock the client, real QueryClientProvider)**

```tsx
// web/src/api/libraryHooks.test.tsx
import { describe, expect, it, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

vi.mock('./client', () => ({
  ApiRequestError: class extends Error {},
  api: {
    get: vi.fn().mockImplementation((path: string) => {
      if (path === '/library/plugins') {
        return Promise.resolve({ plugins: [{ id: 'a@m', name: 'a', marketplace: 'm', enabled: true, components: { commands: 0, skills: 0, agents: 0, mcpServers: 0, hooks: 0 } }], marketplaces: [] })
      }
      return Promise.resolve({})
    }),
    patch: vi.fn().mockResolvedValue({ id: 'a@m', enabled: false }),
    put: vi.fn(), del: vi.fn(), post: vi.fn(),
  },
}))

import { useLibraryPlugins, useTogglePlugin } from './hooks'
import { api } from './client'

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('library hooks', () => {
  it('fetches plugins', async () => {
    const { result } = renderHook(() => useLibraryPlugins(), { wrapper })
    await waitFor(() => expect(result.current.data?.plugins[0].id).toBe('a@m'))
  })

  it('toggle PATCHes the plugin', async () => {
    const { result } = renderHook(() => useTogglePlugin(), { wrapper })
    result.current.mutate({ id: 'a@m', enabled: false })
    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith('/library/plugins/a%40m', { enabled: false }))
  })
})
```

- [ ] **Step 4: Verify**

Run in `web/`: `npm run test -- libraryHooks` then `npm run typecheck`
Expected: PASS, no type errors.

- [ ] **Step 5: Commit**

```bash
git add web/src/api/types.ts web/src/api/hooks.ts web/src/api/libraryHooks.test.tsx
git commit -m "feat(library): frontend types + react-query hooks for the library API"
```

---

### Task 6: Tab plumbing + `LibraryView` shell

**Files:**
- Modify: `web/src/components/Dashboard.tsx:461` (TabId union) and the panel block after the insights panel (~line 899)
- Modify: `web/src/components/DesktopShell.tsx:72` (TabId union) and the `mainPane` chain (~line 451)
- Modify: `web/src/components/tabDefs.ts:19` (TAB_DEFS)
- Modify: `web/src/App.tsx` (route `/library` after `/insights`, line ~134)
- Create: `web/src/components/library/LibraryView.tsx`
- Test: `web/src/components/library/LibraryView.test.tsx`

**Interfaces:**
- Consumes: hooks from Task 5; section components arrive in Tasks 7–9, so this task creates them as minimal placeholders that later tasks replace: `PluginsSection.tsx`, `SkillsSection.tsx`, `CommandsSection.tsx`, `McpSection.tsx`, each default-rendering its list via the Task-5 list hook (name + description rows only, no interactions yet).
- Produces: `LibraryView` — section switcher persisted in the `?section=` search param (`plugins` default, `skills`, `commands`, `mcp`), exported `type LibrarySection`.

- [ ] **Step 1: Write the failing test**

```tsx
// web/src/components/library/LibraryView.test.tsx
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('@/api/client', () => ({
  ApiRequestError: class extends Error {},
  api: {
    get: vi.fn().mockImplementation((path: string) => {
      if (path === '/library/plugins') {
        return Promise.resolve({
          plugins: [{ id: 'alpha@m', name: 'alpha', marketplace: 'm', enabled: true, description: 'Alpha plugin', components: { commands: 1, skills: 0, agents: 0, mcpServers: 0, hooks: 0 } }],
          marketplaces: [],
        })
      }
      if (path === '/library/skills') return Promise.resolve({ skills: [{ name: 'my-skill', source: 'user', readonly: false }] })
      if (path === '/library/commands') return Promise.resolve({ commands: [] })
      if (path === '/library/mcp') return Promise.resolve({ servers: [] })
      return Promise.resolve({})
    }),
    post: vi.fn(), put: vi.fn(), patch: vi.fn(), del: vi.fn(),
  },
}))

import { LibraryView } from './LibraryView'

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/library']}>
        <LibraryView />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('LibraryView', () => {
  it('defaults to the Plugins section and lists plugins', async () => {
    renderView()
    expect(await screen.findByText('alpha')).toBeTruthy()
  })

  it('switches sections', async () => {
    renderView()
    await userEvent.click(screen.getByRole('tab', { name: /skills/i }))
    expect(await screen.findByText('my-skill')).toBeTruthy()
  })
})
```

- [ ] **Step 2: Run to verify it fails** — `cd web && npm run test -- LibraryView` → FAIL (module missing).

- [ ] **Step 3: Implement `LibraryView` + placeholder sections**

```tsx
// web/src/components/library/LibraryView.tsx
/**
 * Library — manage plugins, skills, slash commands, and MCP servers.
 * These live in the SHARED config base (mirrored into every account), so
 * everything here applies to all accounts; changes take effect for newly
 * started sessions.
 */
import { useSearchParams } from 'react-router-dom'
import { PluginsSection } from './PluginsSection'
import { SkillsSection } from './SkillsSection'
import { CommandsSection } from './CommandsSection'
import { McpSection } from './McpSection'

export type LibrarySection = 'plugins' | 'skills' | 'commands' | 'mcp'

const SECTIONS: { id: LibrarySection; label: string }[] = [
  { id: 'plugins', label: 'Plugins' },
  { id: 'skills', label: 'Skills' },
  { id: 'commands', label: 'Commands' },
  { id: 'mcp', label: 'MCP Servers' },
]

export function LibraryView() {
  const [params, setParams] = useSearchParams()
  const raw = params.get('section')
  const section: LibrarySection = SECTIONS.some((s) => s.id === raw) ? (raw as LibrarySection) : 'plugins'

  return (
    <div className="max-w-3xl mx-auto w-full">
      <div role="tablist" aria-label="Library sections" className="flex gap-1 mb-4 border-b border-[var(--border)]">
        {SECTIONS.map((s) => (
          <button
            key={s.id}
            role="tab"
            aria-selected={section === s.id}
            className={`px-3 py-2 text-sm rounded-t-md min-h-[44px] ${
              section === s.id
                ? 'font-semibold border-b-2 border-current'
                : 'opacity-70 hover:opacity-100'
            }`}
            onClick={() => setParams((p) => { p.set('section', s.id); return p }, { replace: true })}
          >
            {s.label}
          </button>
        ))}
      </div>
      {section === 'plugins' && <PluginsSection />}
      {section === 'skills' && <SkillsSection />}
      {section === 'commands' && <CommandsSection />}
      {section === 'mcp' && <McpSection />}
    </div>
  )
}
```

Placeholder sections (Tasks 7–9 replace these; keep them rendering real list data so the Task-6 test is honest). Example — repeat the same shape for all four:

```tsx
// web/src/components/library/PluginsSection.tsx  (placeholder, replaced in Task 7)
import { useLibraryPlugins } from '@/api/hooks'

export function PluginsSection() {
  const { data, isLoading } = useLibraryPlugins()
  if (isLoading) return <div className="text-sm opacity-70 p-4">Loading…</div>
  return (
    <ul className="space-y-2">
      {(data?.plugins ?? []).map((p) => (
        <li key={p.id} className="border border-[var(--border)] rounded-lg p-3">
          <span className="font-medium">{p.name}</span>
          {p.description && <p className="text-sm opacity-70">{p.description}</p>}
        </li>
      ))}
    </ul>
  )
}
```

`SkillsSection` maps `data?.skills` keyed by `${s.source}/${s.name}` showing `s.name`; `CommandsSection` maps `data?.commands` keyed by `${c.source}/${c.path}` showing `/{c.path.replace(/\//g, ':')}`; `McpSection` maps `data?.servers` keyed by `${s.source}/${s.name}` showing `s.name`. Use hooks `useLibrarySkills`, `useLibraryCommands`, `useLibraryMcp`.

Plumbing edits (each mirrors the `insights` handling exactly — copy the neighbor):
1. `tabDefs.ts`: insert `{ id: 'library', label: 'Library' }` between `automation` and `insights`.
2. `Dashboard.tsx:461`: add `'library'` to the `TabId` union. After the insights panel block (~line 899) add:
   ```tsx
   {/* Library panel */}
   {activeTab === 'library' && (
     <div className="flex-1 min-h-0 overflow-y-auto pb-4 px-4">
       <LibraryView />
     </div>
   )}
   ```
   (The `flex-1 min-h-0 overflow-y-auto` wrapper is required — mobile tab panels don't scroll without it.)
3. `DesktopShell.tsx:72`: add `'library'` to `TabId`; in the `mainPane` chain add:
   ```tsx
   } else if (tab === 'library') {
     mainPane = <div className="p-4 overflow-y-auto h-full"><LibraryView /></div>
   ```
4. `App.tsx`: add `<Route path="/library" element={<DashboardRoute tab="library" />} />` after the insights route. Check how `DesktopShell` maps tab→URL (search for where existing tab navigation happens — likely shared via `onTabChange` → `navigate`) and confirm `library` flows through the same generic mapping (`/${next}`); it does if the code uses the tab id as the path segment.

- [ ] **Step 4: Verify** — `cd web && npm run test && npm run typecheck && npm run lint`

Adding a tab may break existing tests that enumerate tabs (App.test.tsx, Dashboard/TabBar tests). Fix them by updating expected tab lists/counts — do NOT delete assertions.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/library/ web/src/components/tabDefs.ts web/src/components/Dashboard.tsx web/src/components/DesktopShell.tsx web/src/App.tsx <any fixed tests>
git commit -m "feat(library): Library tab + section shell (plugins/skills/commands/mcp)"
```

---

### Task 7: Plugins section — toggle + detail

**Files:**
- Replace: `web/src/components/library/PluginsSection.tsx`
- Test: `web/src/components/library/PluginsSection.test.tsx`

**Interfaces:**
- Consumes: `useLibraryPlugins`, `useTogglePlugin`, `useLibraryPluginDetail`

Full component:

```tsx
// web/src/components/library/PluginsSection.tsx
import { useState } from 'react'
import { useLibraryPluginDetail, useLibraryPlugins, useTogglePlugin } from '@/api/hooks'
import type { LibraryPlugin } from '@/api/types'

export function PluginsSection() {
  const { data, isLoading } = useLibraryPlugins()
  if (isLoading) return <div className="text-sm opacity-70 p-4">Loading…</div>
  const plugins = data?.plugins ?? []
  const marketplaces = data?.marketplaces ?? []
  return (
    <div>
      <p className="text-xs opacity-60 mb-3">
        Plugins are shared across all accounts. Enable/disable applies to newly started sessions.
      </p>
      <ul className="space-y-2">
        {plugins.map((p) => <PluginRow key={p.id} plugin={p} />)}
        {plugins.length === 0 && (
          <li className="text-sm opacity-70 p-4">No plugins installed.</li>
        )}
      </ul>
      {marketplaces.length > 0 && (
        <div className="mt-6 text-xs opacity-60">
          Marketplaces: {marketplaces.map((m) => m.name).join(', ')}
        </div>
      )}
    </div>
  )
}

function PluginRow({ plugin }: { plugin: LibraryPlugin }) {
  const [expanded, setExpanded] = useState(false)
  const toggle = useTogglePlugin()
  const detail = useLibraryPluginDetail(plugin.id, expanded)
  const c = plugin.components
  const componentSummary = [
    c.commands && `${c.commands} commands`,
    c.skills && `${c.skills} skills`,
    c.agents && `${c.agents} agents`,
    c.mcpServers && `${c.mcpServers} MCP`,
    c.hooks && `${c.hooks} hooks`,
  ].filter(Boolean).join(' · ')

  return (
    <li className="border border-[var(--border)] rounded-lg p-3">
      <div className="flex items-center gap-3">
        <button
          className="flex-1 min-w-0 text-left"
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
        >
          <div className="flex items-baseline gap-2 min-w-0">
            <span className="font-medium truncate">{plugin.name}</span>
            {plugin.version && <span className="text-xs opacity-60">{plugin.version}</span>}
            {plugin.category && (
              <span className="text-xs px-1.5 py-0.5 rounded-full border border-[var(--border)] opacity-70">
                {plugin.category}
              </span>
            )}
          </div>
          {plugin.description && (
            <p className="text-sm opacity-70 truncate">{plugin.description}</p>
          )}
          {componentSummary && <p className="text-xs opacity-50 mt-0.5">{componentSummary}</p>}
        </button>
        <label className="flex items-center gap-2 shrink-0 cursor-pointer min-h-[44px]">
          <span className="text-xs opacity-60">{plugin.enabled ? 'On' : 'Off'}</span>
          <input
            type="checkbox"
            role="switch"
            aria-label={`Enable ${plugin.name}`}
            checked={plugin.enabled}
            disabled={toggle.isPending}
            onChange={(e) => toggle.mutate({ id: plugin.id, enabled: e.target.checked })}
          />
        </label>
      </div>
      {expanded && (
        <div className="mt-3 pt-3 border-t border-[var(--border)] text-sm space-y-2">
          {detail.isLoading && <div className="opacity-70">Loading components…</div>}
          {detail.data && (
            <>
              <ComponentList label="Commands" items={detail.data.commands.map((x) => `/${x.path.replace(/\//g, ':')}`)} />
              <ComponentList label="Skills" items={detail.data.skills.map((x) => x.name)} />
              <ComponentList label="Agents" items={detail.data.agents} />
              <ComponentList label="MCP servers" items={detail.data.mcpServers.map((x) => `${x.name} (${x.transport})`)} />
              {detail.data.hasHooks && <div className="opacity-70">Provides lifecycle hooks.</div>}
              <div className="text-xs opacity-50">{plugin.marketplace} · updated {plugin.lastUpdated ?? 'unknown'}</div>
            </>
          )}
        </div>
      )}
    </li>
  )
}

function ComponentList({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null
  return (
    <div>
      <span className="font-medium">{label}:</span>{' '}
      <span className="opacity-80">{items.join(', ')}</span>
    </div>
  )
}
```

- [ ] **Step 1: Write the test first** (mock `@/api/client` as in Task 6; assert: rows render; clicking the switch calls `api.patch('/library/plugins/alpha%40m', {enabled:false})`; expanding fetches `/library/plugins/alpha@m` detail and shows a command name).
- [ ] **Step 2: Run to verify it fails**, **Step 3: implement** (code above), **Step 4:** `npm run test -- PluginsSection && npm run typecheck && npm run lint`
- [ ] **Step 5: Commit** — `feat(library): plugins section with enable toggle and component detail`

---

### Task 8: Skills + Commands sections with markdown editor

**Files:**
- Create: `web/src/components/library/ItemEditorDialog.tsx`
- Replace: `web/src/components/library/SkillsSection.tsx`, `web/src/components/library/CommandsSection.tsx`
- Test: `web/src/components/library/SkillsSection.test.tsx` (covers the editor; commands section is structurally identical)

**Interfaces:**
- Consumes: `useLibrarySkills`, `useLibraryCommands`, `useLibraryItemContent`, `useSaveLibraryItem`, `useDeleteLibraryItem`
- Produces: `ItemEditorDialog` props:
  ```ts
  interface ItemEditorDialogProps {
    kind: 'skills' | 'commands'
    /** null = create-new mode (dialog asks for a name/path) */
    id: string | null
    /** plugin id when viewing a read-only plugin item */
    plugin?: string
    readonly?: boolean
    onClose: () => void
  }
  ```

Implementation notes (complete code follows the same patterns already shown; the implementer writes it):
- `ItemEditorDialog`: fixed-overlay dialog copied structurally from `AddAccountDialog.tsx` (backdrop `fixed inset-0 bg-black/50`, `role="dialog" aria-modal="true"`, Escape closes, focus the first field on mount). Body: in create mode a name input (`placeholder` `my-skill` / `project/my-command`) + starter template pre-filled in the textarea:
  - skills: `---\nname: <name>\ndescription: \n---\n\n`
  - commands: `---\ndescription: \n---\n\n`
  A monospace `<textarea aria-label="Content" className="font-mono text-sm w-full h-72 …">` bound to state, initialized from `useLibraryItemContent(kind, id, plugin)` when editing. Footer: Cancel · Delete (edit mode, non-readonly — uses `window.confirm` before `useDeleteLibraryItem`) · Save (`useSaveLibraryItem`, disabled while pending or when create-mode name fails `^[a-z0-9][a-z0-9-]*$` for skills / segment regex for commands). Readonly mode: textarea `readOnly`, no Save/Delete, title says "View (read-only)". On successful save/delete call `onClose()`.
- `SkillsSection`: groups rows `source === 'user'` first under a "Your skills" heading with a "New skill" button, then plugin skills grouped under "From plugins" with the plugin id as a badge. Each row: name, description, `parseError` warning badge if set; click opens `ItemEditorDialog` (readonly for plugin rows, passing `plugin={s.source}`).
- `CommandsSection`: identical structure; rows display `/{path.replace(/\//g, ':')}` plus description; "New command".

- [ ] **Step 1: Write the failing test** — mock `@/api/client`; cases: (a) user skill row + plugin skill row render in their groups; (b) clicking "New skill", typing a name + content, clicking Save calls `api.put('/library/skills/<name>', {content})`; (c) opening an existing user skill loads content via `api.get('/library/skills/my-skill')` and Delete (with `vi.spyOn(window, 'confirm').mockReturnValue(true)`) calls `api.del`; (d) plugin skill opens readonly — no Save button.
- [ ] **Step 2: Run to verify failure. Step 3: Implement. Step 4: `npm run test -- SkillsSection && npm run typecheck && npm run lint`.**
- [ ] **Step 5: Commit** — `feat(library): skills & commands sections with markdown editor dialog`

---

### Task 9: MCP servers section

**Files:**
- Replace: `web/src/components/library/McpSection.tsx`
- Create: `web/src/components/library/McpServerDialog.tsx`
- Test: `web/src/components/library/McpSection.test.tsx`

**Interfaces:**
- Consumes: `useLibraryMcp`, `useSaveMcpServer`, `useDeleteMcpServer`
- Produces: `McpServerDialog` props `{ server: LibraryMcpServer | null /* null = create */, onClose: () => void }`

Implementation notes:
- `McpSection`: note line "MCP servers are shared across accounts; changes apply to newly started sessions." Rows: name, transport badge (`stdio`/`http`/`sse`), source badge (`user` vs plugin id), amber `needs auth: acct1, acct2` badge when `needsAuth.length > 0`. "Add server" button. User rows clickable → edit dialog; plugin rows → readonly dialog (config shown as pretty-printed JSON in a readOnly textarea).
- `McpServerDialog` (same dialog chrome as Task 8): 
  - Form mode (default): name input (create only, regex `^[A-Za-z0-9][A-Za-z0-9._-]*$`), transport `<select>` (stdio/http/sse); stdio → command input, args textarea (one per line), env textarea (`KEY=VALUE` per line); http/sse → url input, headers textarea (`Name: value` per line).
  - "Edit as JSON" toggle: textarea with `JSON.stringify(config, null, 2)`; on save `JSON.parse` (inline error message on parse failure). The two modes edit the same state; build `config` from form fields on save: stdio `{type:'stdio', command, args?, env?}` (omit empty), http/sse `{type, url, headers?}`.
  - Save → `useSaveMcpServer().mutate({name, config})`; surface `ApiRequestError` body `detail.message` as inline error. Delete (edit mode) with `window.confirm`.
- [ ] **Step 1: failing test** — cases: (a) user + plugin rows render with transport and needs-auth badges; (b) Add server → fill name `srv`, transport http, url → Save calls `api.put('/library/mcp/srv', {config:{type:'http',url:'https://x/mcp'}})`; (c) delete flow calls `api.del('/library/mcp/srv')`; (d) plugin server dialog has no Save.
- [ ] **Step 2: Run to verify failure. Step 3: Implement. Step 4: `npm run test -- McpSection && npm run typecheck && npm run lint`.**
- [ ] **Step 5: Commit** — `feat(library): MCP servers section with typed form + JSON editor`

---

### Task 10: Full verification + PR

- [ ] **Step 1: Full server suite** (deselect the known local-only failures per project memory):

```bash
env -u CCSWITCH_TAILNET_ALLOWLIST CCSWITCH_DEV=1 PYTHONPATH=.:src python3 -m pytest server/tests/ \
  --ignore=server/tests/test_daemon_properties.py \
  --ignore=server/tests/test_eta_parity_live.py \
  --ignore=server/tests/test_copilot_proxy_resolve.py -q
```

Expected: PASS (do not pipe through `tail` — collection errors hide). Known CI-flaky tests (`test_batch_unarchive_partial_failure`, drain-cancel in `test_rearm_drain_snapshot`) are not regressions.

- [ ] **Step 2: Lint + types**: `python3 -m ruff check server/ src/` (or `ruff check` if on PATH) and `PYTHONPATH=.:src python3 -m mypy server/ --ignore-missing-imports`.
- [ ] **Step 3: Full web suite**: in `web/`: `npm run test && npm run typecheck && npm run lint`.
- [ ] **Step 4: Push and open the PR** (non-draft, auto-merge — both required):

```bash
git push -u origin feat/library-ui
gh pr create --title "feat(web): Library tab — manage plugins, skills, commands, MCP servers" --body "$(cat <<'EOF'
## Summary
- New top-level **Library** tab: view/manage the shared Claude Code extension surface across all accounts
- **Plugins**: inventory with catalog metadata, enable/disable (settings.json enabledPlugins), component detail
- **Skills & Commands**: full CRUD over the shared skills/ and commands/ dirs with a markdown editor
- **MCP servers**: full CRUD over store/claude.shared.json mcpServers (typed form + raw JSON), plugin servers read-only, per-account needs-auth badges
- Backend: server/library/ scan+mutate package + /api/library router; atomic writes, path-traversal guards, audit events

Design: docs/superpowers/specs/2026-07-12-library-ui-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
gh pr merge --squash --auto
```

---

## Self-review notes

- Spec coverage: plugins list/toggle/detail (T2/T4/T7), skills CRUD (T2/T3/T4/T8), commands CRUD (same), MCP CRUD + needs-auth badges (T2/T3/T4/T9), tab integration + mobile scroll (T6), marketplaces read-only (T2/T7), error conventions + audit (T4), traversal defense (T1/T3/T4 tests).
- Deliberately out of scope (per spec): plugin install/uninstall, per-project MCP config, hooks/agents management.
- Type consistency: `LibraryItemKind = 'skills' | 'commands'` used by content/save/delete hooks; section components consume exactly the Task-5 hook names.
