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
