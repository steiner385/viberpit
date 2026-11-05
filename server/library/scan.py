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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from viberpit import paths
from server.library import plugin_policy
from server.library.names import resolve_under, valid_plugin_id
from viberpit.mcp_names import short_name

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


def newest_install(entries: list[dict[str, Any]]) -> dict[str, Any]:
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
        install = newest_install(entries).get("installPath")
        if install and Path(install).is_dir():
            out.append((pid, Path(install)))
    return out


def enabled_plugin_names() -> frozenset[str]:
    """The names the CLI keys enabled plugins' MCP servers by (``plugin:<name>:<server>``)."""
    return frozenset(_plugin_manifest_name(install, pid.partition("@")[0])
                     for pid, install in enabled_plugin_paths())


def known_marketplaces() -> dict[str, dict[str, Any]]:
    """``plugins/known_marketplaces.json`` on this host: ``{name: {source, installLocation,
    lastUpdated, autoUpdate?}}``; malformed entries dropped."""
    raw = read_json_safe(paths.shared_base() / "plugins" / "known_marketplaces.json") or {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)}


def list_plugins() -> dict[str, Any]:
    enabled = _enabled_map()
    registry = installed_registry()
    catalog = _catalog()
    markets = known_marketplaces()
    plugins: list[dict[str, Any]] = []
    for pid in sorted(set(registry) | set(enabled)):
        newest = newest_install(registry.get(pid, []))
        cat = catalog.get(pid) or {}
        entry = cat.get("marketplace_entry") or {}
        components = cat.get("components") or {}
        name, _, marketplace = pid.partition("@")
        tokens = cat.get("tokens")
        token_cost: dict[str, int] | None = None
        if tokens and isinstance(tokens, dict):
            max_always_on = max(
                (v.get("always_on", 0) for v in tokens.values() if isinstance(v, dict)),
                default=0,
            )
            max_on_invoke = max(
                (v.get("on_invoke", 0) for v in tokens.values() if isinstance(v, dict)),
                default=0,
            )
            token_cost = {"alwaysOn": max_always_on, "onInvoke": max_on_invoke}
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
            "tokenCost": token_cost,
            # D3: the marketplace's source, and whether the CLI auto-updates it on startup.
            "source": (markets.get(marketplace) or {}).get("source"),
            "autoUpdate": plugin_policy.auto_updates(marketplace, markets.get(marketplace)),
        })
    marketplaces = [
        {"name": name, "source": m.get("source"), "lastUpdated": m.get("lastUpdated"),
         "autoUpdate": plugin_policy.auto_updates(name, m)}
        for name, m in sorted(markets.items())
    ]
    return {"plugins": plugins, "marketplaces": marketplaces}


# --------------------------------------------------------------------------- browse (D-16c)

BROWSE_DESCRIPTION_CAP = 500
_SOURCE_CAP = 300  # a Directory repository URL on the consent card
BROWSE_LIMIT_MAX = 100
_DIRECTORY_CACHE = "plugin-directory-cache-v2.json"
# Parsed Directory cache keyed by the file's (mtime_ns, size): the file is 2.7 MB.
_directory_memo: tuple[tuple[int, int], dict[str, dict[str, Any]]] | None = None


def directory_cache_path() -> Path:
    return paths.shared_base() / "plugins" / _DIRECTORY_CACHE


def directory_listings() -> dict[str, dict[str, Any]] | None:
    """The Anthropic Directory cache by entry name (the ``<name>`` of
    ``<name>@anthropic-plugin-directory``), or ``None`` when the cache is missing or
    unreadable — which every caller treats as "nothing is reviewed"."""
    global _directory_memo
    path = directory_cache_path()
    try:
        st = path.stat()
    except OSError:
        return None
    key = (st.st_mtime_ns, st.st_size)
    if _directory_memo is not None and _directory_memo[0] == key:
        return _directory_memo[1]
    data = read_json_safe(path)
    if data is None:
        return None
    names = data.get("entryNames")
    listings = data.get("listings")
    if not isinstance(names, dict) or not isinstance(listings, list):
        return None
    out: dict[str, dict[str, Any]] = {}
    for listing in listings:
        if not isinstance(listing, dict):
            continue
        name = names.get(listing.get("id"))
        if isinstance(name, str) and name and name not in out:
            out[name] = listing
    _directory_memo = (key, out)
    return out


def marketplace_catalog(name: str, config: dict[str, Any] | None = None) -> dict[str, dict[str, Any]] | None:
    """A configured marketplace's own catalog — ``<installLocation>/.claude-plugin/
    marketplace.json`` — as ``{plugin name: entry}``, or ``None`` when it cannot be read."""
    cfg = config if config is not None else known_marketplaces().get(name)
    location = (cfg or {}).get("installLocation")
    if not isinstance(location, str) or not location:
        return None
    data = read_json_safe(Path(location) / ".claude-plugin" / "marketplace.json")
    plugins = (data or {}).get("plugins")
    if not isinstance(plugins, list):
        return None
    return {p["name"]: p for p in plugins
            if isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"]}


def marketplace_sources(markets: Mapping[str, dict[str, Any]] | None = None) -> dict[str, str | None]:
    """Each configured marketplace's ``plugin_policy.normalize_source`` on THIS host."""
    markets = known_marketplaces() if markets is None else markets
    return {name: plugin_policy.normalize_source(cfg.get("source")) for name, cfg in markets.items()}


def install_catalog(curated: Mapping[str, str]) -> plugin_policy.InstallCatalog:
    """What ``plugin_policy.check_install`` judges against on this host: every configured
    marketplace's normalized source, each curated marketplace's catalog that reads, and
    the Directory cache."""
    markets = known_marketplaces()
    catalogs = {}
    for name in sorted(curated):
        if name in markets:
            entries = marketplace_catalog(name, markets[name])
            if entries is not None:
                catalogs[name] = entries
    return plugin_policy.InstallCatalog(
        marketplaces=catalogs, directory=directory_listings(), sources=marketplace_sources(markets))


def _cap(text: object) -> str | None:
    return text[:BROWSE_DESCRIPTION_CAP] if isinstance(text, str) else None


def _browse_components(cat: dict[str, Any] | None) -> dict[str, int] | None:
    comps = (cat or {}).get("components")
    if not isinstance(comps, dict):
        return None  # unknown: the UI says "Contents unknown"
    return {k: len(comps.get(k) or []) for k in ("hooks", "mcpServers", "lspServers", "agents", "skills")}


def browse_entries(curated: Mapping[str, str]) -> list[dict[str, Any]]:
    """Every installable plugin: confirmed marketplaces whose source is unchanged
    (``plugin_policy.check_curated``), minus command-source entries and entries whose
    source fetches from a refused address (``entry_source_refusal``), plus REVIEWED
    Directory listings only. Unreviewed listings never leave here."""
    installed = set(installed_registry())
    catalog = _catalog()
    markets = known_marketplaces()
    sources = marketplace_sources(markets)
    out: list[dict[str, Any]] = []
    for mkt in sorted(curated):
        if mkt not in markets or mkt == plugin_policy.DIRECTORY_MARKETPLACE:
            continue
        try:
            plugin_policy.check_curated(mkt, curated, sources)
        except plugin_policy.PolicyRefusal:
            continue
        for name, entry in sorted((marketplace_catalog(mkt, markets[mkt]) or {}).items()):
            pid = f"{name}@{mkt}"
            if (not valid_plugin_id(pid) or plugin_policy.is_command_source(entry)
                    or plugin_policy.entry_source_refusal(entry)):
                continue
            out.append({
                "pluginId": pid, "name": name, "description": _cap(entry.get("description")),
                "marketplace": mkt, "version": entry.get("version") if isinstance(entry.get("version"), str) else None,
                "reviewed": False, "components": _browse_components(catalog.get(pid)),
                "installed": pid in installed,
                "source": None,  # the marketplace's own: the UI shows its normalizedSource
            })
    for name, listing in sorted((directory_listings() or {}).items()):
        pid = f"{name}@{plugin_policy.DIRECTORY_MARKETPLACE}"
        if (not plugin_policy.is_reviewed(listing) or not valid_plugin_id(pid)
                or plugin_policy.is_command_source(listing)):
            continue
        raw_release = listing.get("release")
        release: dict[str, Any] = raw_release if isinstance(raw_release, dict) else {}
        out.append({
            "pluginId": pid,
            "name": listing.get("name") if isinstance(listing.get("name"), str) else name,
            "description": _cap(listing.get("summary")),
            "marketplace": plugin_policy.DIRECTORY_MARKETPLACE,
            "version": release.get("version") if isinstance(release.get("version"), str) else None,
            "reviewed": True, "components": _browse_components(catalog.get(pid)),
            "installed": pid in installed,
            "source": _directory_source(listing),
        })
    return out


def _directory_source(listing: Mapping[str, Any]) -> str | None:
    """What a Directory listing installs from, for the consent card: its repository URL
    (``source.repository.url``, the shape every 2.1.318 listing carries) pinned to the
    ``reviewed_commit`` when the listing names one; ``None`` when either is unreadable."""
    src = listing.get("source")
    repo = src.get("repository") if isinstance(src, Mapping) else None
    url = repo.get("url") if isinstance(repo, Mapping) else None
    if not isinstance(url, str) or not url.isascii() or not url.isprintable():
        return None
    url = url[:_SOURCE_CAP]
    commit = repo.get("reviewed_commit") if isinstance(repo, Mapping) else None
    if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{7,64}", commit):
        return f"{url} @ {commit[:12]}"
    return url


def browse_page(entries: list[dict[str, Any]], *, q: str | None, marketplace: str | None,
                offset: int, limit: int) -> dict[str, Any]:
    """Server-side search and pagination over :func:`browse_entries`."""
    needle = (q or "").strip().lower()
    rows = [e for e in entries
            if (not marketplace or e["marketplace"] == marketplace)
            and (not needle or needle in e["pluginId"].lower() or needle in str(e["name"]).lower()
                 or needle in (e["description"] or "").lower())]
    return {"plugins": rows[offset:offset + limit], "total": len(rows),
            "offset": offset, "limit": limit}


def _plugin_manifest_name(install: Path, registry_name: str) -> str:
    """Name the CLI keys a plugin's MCP servers by: the manifest name, not the registry id.

    Spike addendum (Task 3): registry notion@claude-plugins-official has manifest name "Notion"
    and the CLI reports plugin:Notion:notion. Falls back to the registry name without a manifest.
    """
    manifest = read_json_safe(install / ".claude-plugin" / "plugin.json")
    name = manifest.get("name") if isinstance(manifest, dict) else None
    return name if isinstance(name, str) and name else registry_name


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


def _output_styles_dir() -> Path:
    """The shared user output-styles dir (D-16a): ``~/.claude/output-styles``, linked
    into every account (``SHARE_ALLOWLIST``), which the CLI reads as
    ``<CLAUDE_CONFIG_DIR>/output-styles/*.md``."""
    return paths.shared_base() / "output-styles"


def _user_output_style(md: Path) -> dict[str, Any]:
    try:
        meta, _err = parse_frontmatter(md.read_text())
    except OSError:
        meta = {}
    name = meta.get("name")
    desc = meta.get("description")
    return {
        # The CLI names a style by its frontmatter `name`, else the filename stem.
        "name": name if isinstance(name, str) and name.strip() else md.stem,
        "source": "user",
        "description": desc if isinstance(desc, str) else None,
        "path": md.name,
    }


def list_output_styles() -> list[dict[str, Any]]:
    """The styles the Settings default may name (D-16a): the CLI's five built-ins, then
    the ``.md`` files in the shared user dir sorted by name. Plugin styles
    (``<plugin>:<name>``) are only ever picked per session, from its CLI's own list."""
    from server.sessions.flag_settings import BUILTIN_OUTPUT_STYLES
    out: list[dict[str, Any]] = [
        {"name": n, "source": "builtin", "description": None} for n in BUILTIN_OUTPUT_STYLES]
    root = _output_styles_dir()
    if root.is_dir():
        users = [_user_output_style(md) for md in root.glob("*.md") if md.is_file()]
        out.extend(sorted(users, key=lambda s: (s["name"].lower(), s["path"])))
    return out


def read_output_style(path: str) -> dict[str, Any] | None:
    """One shared user style file, read-only: ``{name, path, content}``, or None when
    *path* is not a ``.md`` file directly inside the shared dir."""
    if "/" in path or not path.endswith(".md"):
        return None
    file = resolve_under(_output_styles_dir(), path)
    if file is None or not file.is_file():
        return None
    style = _user_output_style(file)
    return {"name": style["name"], "path": file.name, "content": file.read_text()}


def list_mcp_servers(status_rows: list[dict] | None = None) -> dict[str, Any]:
    """Configured MCP servers, each with its live ``status`` rows (mcp_server_status table)."""
    by_short: dict[str, list[dict]] = {}
    for r in status_rows or []:
        by_short.setdefault(short_name(r["server"]), []).append(r)
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
                "status": by_short.get(name, []),
            })
    for pid, install in enabled_plugin_paths():
        plugin_name = _plugin_manifest_name(install, pid.partition("@")[0])
        for name, cfg in sorted(_plugin_mcp_servers(install).items()):
            servers.append({
                "name": name, "source": pid, "pluginName": plugin_name, "readonly": True,
                "transport": infer_transport(cfg), "config": cfg,
                "status": by_short.get(name, []),
            })
    return {"servers": servers}


SIGN_IN_TRANSPORTS = ("http", "sse")


class SignInServerError(Exception):
    """Why *name* cannot be signed in to: ``reason`` is ``not_found``, ``ambiguous`` or
    ``not_oauth`` (not an http/sse server)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def sign_in_server(servers: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """THE server-name match for a browser sign-in (paste-back and one-tap): the one
    configured server named exactly *name*, which must be http/sse. Raises
    :class:`SignInServerError` otherwise."""
    matches = [s for s in servers if s.get("name") == name]
    if not matches:
        raise SignInServerError("not_found")
    if len(matches) != 1:
        raise SignInServerError("ambiguous")
    if matches[0].get("transport") not in SIGN_IN_TRANSPORTS:
        raise SignInServerError("not_oauth")
    return matches[0]
