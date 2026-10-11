"""Safety assessment for standing permission rules: what may be stored, and what needs a nudge.

Pure. A rule is judged by what it would *allow*, so Bash and path rules are probed with
representative dangerous requests through the real matcher (``patterns.matches_any``) rather than
by inspecting the pattern text. The Bash probe set is tied to the autopilot baseline deny list by
a test, so the two cannot drift apart. Spec section 4 of
``docs/superpowers/specs/2026-10-11-always-allow-permission-rules-design.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from server.permission_rules.patterns import (
    _PATH_TOOLS,
    Pattern,
    PatternError,
    describe,
    matches_any,
    parse,
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
    ".env",
    "app/.env.local",
    ".git/config",
    ".claude/settings.json",
    ".ssh/id_rsa",
    "certs/server.pem",
    "certs/server.key",
    "credentials.json",
)

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


def _assess_bash(p: Pattern) -> Assessment:
    spec = p.spec or ""
    if p.kind == "bare" or (p.kind == "prefix" and not spec) or (p.kind == "glob" and spec == "*"):
        return _refuse("allows every shell command")
    if any(tok in spec for tok in _UNMATCHABLE_BASH):
        return _refuse("pattern can never match a command")
    for probe in _DANGER_PROBES:
        if matches_any([p], "Bash", {"command": probe}, _PROBE_CWD):
            return _refuse(f"would allow a dangerous command, e.g. `{probe}`")
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
    for path in _SECRET_PROBES:
        if matches_any([p], p.tool, {"file_path": path}, _PROBE_CWD):
            return _refuse(f"would allow writing secrets or tool config, e.g. {path}")
    if matches_any([p], p.tool, {"file_path": _WORKFLOW_PROBE}, _PROBE_CWD):
        return _verdict(["can change CI workflows"])
    return _verdict([])


def assess(pattern: str, scope: str) -> Assessment:
    if scope not in SCOPES:
        raise ValueError(f"unknown scope: {scope!r}")
    try:
        p = parse(pattern)
    except PatternError as exc:
        return _refuse(f"invalid pattern: {exc}")
    if p.kind == "mcp":
        if p.tool.count("__") == 1:
            return _verdict(["every tool on the server"])
        return _verdict([])
    if p.tool == "Bash":
        return _assess_bash(p)
    if p.tool in _PATH_TOOLS:
        return _assess_path(p, scope)
    if p.kind == "domain":
        if p.spec in ("", "*"):
            return _refuse("domain:* allows every host")
        return _verdict([])
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
                {"pattern": candidate, "description": describe(candidate), "level": a.level}
            )
    return out
