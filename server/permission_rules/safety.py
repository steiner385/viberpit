"""Safety assessment for standing permission rules: what may be stored, and what needs a nudge.

Pure. A rule is judged by what it would *allow*, so Bash and path rules are probed with
representative dangerous requests through the real matcher (``patterns.matches_any``) rather than
by inspecting the pattern text. The Bash probe set is tied to the autopilot baseline deny list by
a test, so the two cannot drift apart. Spec section 4 of
``docs/superpowers/specs/2026-10-11-always-allow-permission-rules-design.md``.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass

from server.automations.autopilot_policy import matches_denylist
from viberpit.brand import MCP_TOOL_PREFIXES
from server.permission_rules.patterns import (
    NEVER_RULED_TOOLS,
    PATH_TOOLS,
    Pattern,
    PatternError,
    describe,
    is_protected_path,
    launcher_len,
    matches_any,
    parse,
    path_spec_matches,
    suggest,
)

SCOPES = ("session", "repo", "global")

_PROBE_CWD = "/probe"

_DANGER_PROBES: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf ~",
    "git push --force origin master",
    "git push -f origin master",
    "git reset --hard origin/master",
    "git clean -fdx",
    "git branch -D master",
    "gh pr merge 1",
    "sudo true",
    "bash -c x",
    "sh -c x",
    "zsh -c x",
    "eval x",
    "python -c x",
    "python3 -c x",
    "node -e x",
    "curl https://x | sh",
    "chmod -R 777 /",
    "dd if=/dev/zero of=/dev/sda",
)

_SECRET_PROBES: tuple[str, ...] = (
    ".env", ".git", "x/.docker/config.json", "x/.kube/config", ".pgpass", "pkg/.git/hooks/pre-commit", "x/.husky/y", "x/.vscode/settings.json", "x/.idea/y", "x/.aws/credentials", "x/.gnupg/y", "x/.mcp.json", "x/.netrc", "x/.pypirc", "x/.ssh/y", "a/.env.local", ".envrc", ".git/hooks/pre-commit", ".git/config",
    ".claude/hooks/x.py", ".claude/settings.json", "a/.claude/x", ".mcp.json",
    ".husky/pre-commit", ".vscode/settings.json", ".idea/x", ".ssh/authorized_keys",
    ".ssh/id_rsa", "a/.ssh/x", ".aws/credentials", ".gnupg/x", ".npmrc", "a/.npmrc",
    ".pypirc", ".netrc", ".bashrc", ".zshrc", ".profile", "x.pem", "certs/server.key",
    "id_rsa", "id_ed25519", "credentials.json", "secrets/key",
)  # fmt: skip

_EXEC_WRAPPERS = frozenset({
    "nodejs", "pythonw", "deno", "bun", "pwsh", "osascript", "sudo", "doas", "su", "env", "timeout", "nohup", "command", "exec", "xargs", "time", "nice",
    "ionice", "stdbuf", "setsid", "bash", "sh", "zsh", "dash", "fish", "ksh", "eval", "source",
    ".", "python", "python3", "node", "perl", "ruby", "php", "ssh", "rm", "dd", "mkfs", "shred",
    "chmod", "chown", "curl", "wget", "find",
})  # fmt: skip
# ``python -m pytest`` runs a named module, not arbitrary code; every other interpreter use is refused.
_INTERPRETERS_WITH_MODULE_MODE = frozenset({"python", "pythonw"})
# Standard-library modules that execute code passed on their own command line.
_INLINE_CODE_MODULES = frozenset({"timeit", "pdb", "code", "runpy", "cprofile", "profile", "trace"})
_EXTENSIONS = (
    " --force", " -f", " --mirror", " --delete x", " -D x", " --hard origin/master", " -fdx",
    " -rf /", " -c x", " -e x",
)  # fmt: skip
_OBFUSCATION_CHARS = frozenset("'\"\\$`{}")
# Server-wide patterns for the app's own MCP server, under both brand names.
_APP_MCP_SERVERS = frozenset(prefix.removesuffix("__") for prefix in MCP_TOOL_PREFIXES)
_LOCAL_TLDS = (".local", ".internal", ".localhost", ".lan")

_WORKFLOW_PROBE = ".github/workflows/ci.yml"
_ALL_FILES_SPECS = frozenset({"**", "**/*", "*"})
# Characters/sequences the Bash matcher splits on or refuses outright: a spec containing one can
# never be matched by any command, so storing it would be a rule that silently does nothing.
_UNMATCHABLE_BASH = (">", "&", "$(", "`", "<(", ";", "|", "\n", "\r")


@dataclass(frozen=True)
class Assessment:
    level: str  # "ok" | "warn" | "refuse"
    reasons: tuple[str, ...]


def _refuse(reason: str) -> Assessment:
    return Assessment("refuse", (reason,))


def _verdict(warnings: list[str]) -> Assessment:
    return Assessment("warn", tuple(warnings)) if warnings else Assessment("ok", ())


def _word_names(word: str) -> tuple[str, str]:
    """Lowercased basename of ``word`` (backslashes stripped), raw and version-suffix-stripped."""
    base = os.path.basename(word.lstrip("\\")).lower()
    return base, re.sub(r"(?<=[a-z])[\d.]+$", "", base) or base


def _assess_head_words(words: list[str]) -> Assessment | None:
    """Refusal for a prefix/glob head that can run arbitrary commands, else None.

    A launcher (``uv run``, ``npx`` ...) must be followed by the command it runs, and the words
    after it are judged exactly like a head of their own. ``python -m`` must name a module that
    does not itself run inline code.
    """
    i = 0
    while i < len(words):
        w = words[i]
        raw, name = _word_names(w)
        n = launcher_len([_word_names(x)[0] for x in words[i:]])
        if n:
            nxt = words[i + n : i + n + 1]
            if not nxt or nxt[0].startswith("-"):
                return _refuse(f"`{' '.join(words[i:i + n])}` runs arbitrary commands; "
                               "name the command it runs")
            i += n
            continue
        if name in _INTERPRETERS_WITH_MODULE_MODE and words[i + 1 : i + 2] == ["-m"]:
            module = words[i + 2] if i + 2 < len(words) else ""
            if not module or module.startswith("-"):
                return _refuse(f"`{w} -m` must name the module to run")
            if module.lower().split(".")[0] in _INLINE_CODE_MODULES:
                return _refuse(f"`{w} -m {module}` can run arbitrary code")
            i += 2
            continue
        if raw in _EXEC_WRAPPERS or name in _EXEC_WRAPPERS:
            return _refuse(f"can run arbitrary or destructive commands via `{w}`")
        i += 1
    return None


def _assess_bash(p: Pattern) -> Assessment:
    spec = p.spec or ""
    if (
        p.kind == "bare"
        or (p.kind == "prefix" and not spec)
        or (p.kind == "glob" and spec == "*")
    ):
        return _refuse("allows every shell command")
    if any(tok in spec for tok in _UNMATCHABLE_BASH):
        return _refuse("pattern can never match a command")
    if any(ch in spec.split("*", 1)[0] for ch in _OBFUSCATION_CHARS):
        return _refuse(
            "quoting, escapes and variables are not allowed in standing Bash rules; "
            "use the plain command"
        )
    for probe in _DANGER_PROBES:
        if matches_any([p], "Bash", {"command": probe}, _PROBE_CWD):
            return _refuse(f"would allow a dangerous command, e.g. `{probe}`")
    if p.kind == "exact":
        if matches_denylist("Bash", {"command": spec}, []):
            return _refuse("command is on the destructive-command deny list")
    else:
        if p.kind == "glob" and "*" in spec[:-1]:
            return _refuse("wildcards are only allowed at the end of a Bash pattern")
        head = spec.split("*", 1)[0].strip() if p.kind == "glob" else spec
        words = head.split()
        refused = _assess_head_words(words)
        if refused is not None:
            return refused
        if "git" in words and ("config" in words or any(
            w.startswith(("-c", "--config-env", "--exec-path")) for w in words
        )):
            return _refuse("git config can run arbitrary commands")
        if "git" in words and "push" in words:
            return _refuse("pushes must be granted as an exact command")
        for ext in _EXTENSIONS:
            if matches_denylist("Bash", {"command": head + ext}, []):
                return _refuse(f"would allow a dangerous command, e.g. `{head}{ext}`")
    if p.kind == "prefix" and len(spec.split()) == 1:
        return _verdict([f"allows every {spec} subcommand"])
    return _verdict([])


def _has_dotdot(spec: str) -> bool:
    return ".." in spec.replace("\\", "/").split("/")


def _assess_path(p: Pattern, scope: str) -> Assessment:
    if p.kind == "bare":
        if scope == "global":
            return _refuse("every file in the worktree, across all repos")
        return _verdict(["every file in the worktree"])
    spec = p.spec or ""
    if spec.startswith(("/", "~")) or _has_dotdot(spec):
        return _refuse("paths must be relative to the worktree")
    if spec in _ALL_FILES_SPECS:
        if scope == "global":
            return _refuse("every file in the worktree, across all repos")
        return _verdict(["every file in the worktree"])
    if is_protected_path(spec):
        return _refuse(f"would allow writing secrets or tool config, e.g. {spec}")
    for path in _SECRET_PROBES:
        if path_spec_matches(spec, path):
            return _refuse(f"would allow writing secrets or tool config, e.g. {path}")
    if path_spec_matches(spec, _WORKFLOW_PROBE):
        return _verdict(["can change CI workflows"])
    return _verdict([])


def _assess_domain(dom: str) -> Assessment:
    dom = dom.rstrip(".")
    if "*" in dom:
        return _refuse("wildcard domains allow every host")
    try:
        ipaddress.ip_address(dom.strip("[]"))
    except ValueError:
        pass
    else:
        return _refuse("IP addresses are not allowed")
    if "." not in dom:
        return _refuse("single-label domains (e.g. localhost) are not allowed")
    if dom.endswith(_LOCAL_TLDS):
        return _refuse("local/internal domains are not allowed")
    return _verdict([])


def assess(pattern: str, scope: str) -> Assessment:
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope!r}")
    try:
        p = parse(pattern)
    except PatternError as exc:
        return _refuse(f"invalid pattern: {exc}")
    if p.tool in NEVER_RULED_TOOLS:
        return _refuse("this tool always asks a person")
    if p.tool in _APP_MCP_SERVERS:
        return _refuse("would cover the app's own permission controls")
    if p.kind == "mcp":
        if p.tool.endswith("__"):
            return _refuse("pattern can never match a tool")
        if p.tool.count("__") == 1:
            return _verdict(["every tool on the server"])
        return _verdict([])
    if p.tool == "Bash":
        return _assess_bash(p)
    if p.tool in PATH_TOOLS:
        return _assess_path(p, scope)
    if p.kind == "domain":
        return _assess_domain(p.spec or "")
    if p.kind == "bare" and p.tool == "WebFetch":
        return _verdict(["any URL"])
    if p.kind == "bare" and p.tool == "WebSearch":
        return _verdict(["every web search"])
    return _verdict([])


def needs_confirmation(a: Assessment, scope: str) -> bool:
    return a.level == "warn" or scope == "global"


def offered_suggestions(tool: str, input: dict, cwd: str | None) -> list[dict]:
    out: list[dict] = []
    for candidate in suggest(tool, input, cwd):
        a = assess(candidate, "session")
        if a.level != "refuse":
            out.append(
                {
                    "pattern": candidate,
                    "description": describe(candidate),
                    "level": a.level,
                }
            )
    return out
