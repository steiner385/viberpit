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
from server.library.names import resolve_under

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
    path = resolve_under(root, name, "SKILL.md")
    if path is None or not path.is_file():
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
    resolved = resolve_under(root, *path.split("/"))
    if resolved is None:
        return None
    file = resolved.with_name(resolved.name + ".md")
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
