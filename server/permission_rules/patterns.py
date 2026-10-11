"""Permission-rule pattern grammar: parse, match, describe and suggest.

Pure apart from one filesystem read: path matching resolves symlinks (``os.path.realpath``) so a
symlink inside the worktree cannot smuggle a path outside it. No database access. Grammar and semantics: spec section 1 of
``docs/superpowers/specs/2026-10-11-always-allow-permission-rules-design.md``.
Safety assessment (what may be stored) lives elsewhere; the matcher here stays honest
about what a pattern literally covers.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from viberpit.brand import MCP_TOOL_PREFIXES

FILE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
PATH_TOOLS = FILE_TOOLS | {"Read"}
# Worktree-relative globs no standing rule may ever cover (secrets, VCS internals, tool config):
# enforced by the matcher itself so even a broad ``Edit(**)`` never auto-allows them.
PROTECTED_PATHS = frozenset(
    {
        "**/.env", "**/.env.*", "**/.envrc", "**/.git", "**/.git/**", "**/.claude/**",
        "**/.mcp.json", "**/.husky/**", "**/.vscode/**", "**/.idea/**", "**/.ssh/**",
        "**/.aws/**", "**/.gnupg/**", "**/.npmrc", "**/.pypirc", "**/.netrc", "**/.bashrc",
        "**/.zshrc", "**/.profile", "**/*.pem", "**/*.key", "**/id_rsa*", "**/id_ed25519*",
        "**/credentials*", "**/.docker/config.json", "**/.kube/**", "**/.pgpass", "**/secrets/**",
    }
)  # fmt: skip
#: Prompts no standing rule may ever answer, in any mode: plan approval and clarifying questions
#: are conversations with the human; the app's own tools that widen what sessions may do
#: without asking (resolve a permission card, change a permission mode, approve a parked model,
#: unpause autopilot, enable an automation); and the ones that settle a human-in-the-loop
#: decision. Under BOTH brand prefixes. The single list read by
#: the matcher, ``suggest``, ``safety.assess`` and ``PermissionRegistry``.
NEVER_RULED_TOOLS = frozenset(
    {"ExitPlanMode", "AskUserQuestion"}
    | {
        f"{prefix}{name}"
        for prefix in MCP_TOOL_PREFIXES
        for name in (
            "resolve_permission", "set_permission_mode", "resolve_model_approval",
            "set_autopilot_paused", "set_automation_enabled",
            # Human-in-the-loop decisions: another session's question, a decision or triage
            # proposal, an initiative stage, a roadmap proposal.
            "answer_question", "resolve_decision", "resolve_triage", "advance_initiative",
            "roadmap_resolve", "roadmap_advance",
        )
    }
)  # fmt: skip
#: Commands whose job is to run the command named after them (``uv run pytest``). A rule must
#: name that command: the launcher alone covers anything, and the rest of the head is judged
#: as a head of its own (``safety``). Suggestions grow by one word past a launcher.
COMMAND_LAUNCHERS: tuple[tuple[str, ...], ...] = (
    ("uv", "run"), ("npm", "exec"), ("npm", "x"), ("pnpm", "exec"), ("pnpm", "dlx"),
    ("yarn", "exec"), ("yarn", "dlx"), ("poetry", "run"), ("pipx", "run"), ("npx",), ("bunx",),
)  # fmt: skip


def launcher_len(names: Sequence[str]) -> int:
    """Number of leading words of ``names`` that form a command launcher (0 if none)."""
    return next((len(lw) for lw in COMMAND_LAUNCHERS if tuple(names[: len(lw)]) == lw), 0)

_TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
# MCP server names routinely contain hyphens/dots (e.g. ``mcp__claude-ai__x``).
_MCP_RE = re.compile(r"^mcp__[A-Za-z0-9][A-Za-z0-9_.-]*$")
_CALL_RE = re.compile(r"([^()]*)\((.*)\)", re.DOTALL)

_SAFE_REDIRECT_RE = re.compile(r"(?:2>&1|&>/dev/null|2>/dev/null|>/dev/null)(?=\s|$|[;&|])")
_SEGMENT_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||\|&|;|\||&|\r|\n)\s*")
_UNSAFE_SUBSTITUTIONS = ("$(", "`", "<(")


class PatternError(ValueError):
    """The pattern string is not valid grammar."""


@dataclass(frozen=True)
class Pattern:
    tool: str
    spec: str | None
    kind: str  # "bare" | "exact" | "prefix" | "glob" | "path" | "domain" | "mcp"


def _squash(text: str) -> str:
    return " ".join(text.split())


def parse(pattern: str) -> Pattern:
    s = pattern.strip()
    if not s:
        raise PatternError("empty pattern")
    if "(" not in s and ")" not in s:
        if s.startswith("mcp__"):
            if not _MCP_RE.match(s):
                raise PatternError(f"invalid MCP tool name: {s!r}")
            return Pattern(s, None, "mcp")
        if not _TOOL_RE.match(s):
            raise PatternError(f"invalid tool name: {s!r}")
        return Pattern(s, None, "bare")
    m = _CALL_RE.fullmatch(s)
    if m is None:
        raise PatternError(f"unbalanced parentheses: {s!r}")
    tool, spec = m.group(1), m.group(2).strip()
    if not _TOOL_RE.match(tool):
        raise PatternError(f"invalid tool name: {tool!r}")
    if not spec:
        raise PatternError("empty spec")
    if tool == "Bash":
        spec = _squash(spec)
        if spec.endswith(":*"):
            spec = spec[:-2].rstrip()
            if not spec:
                raise PatternError("empty command prefix")
            return Pattern(tool, spec, "prefix")
        return Pattern(tool, spec, "glob" if "*" in spec else "exact")
    if tool in PATH_TOOLS:
        return Pattern(tool, spec, "path")
    if tool == "WebFetch":
        if not spec.startswith("domain:") or not spec[7:].strip():
            raise PatternError("WebFetch spec must be domain:<host>")
        return Pattern(tool, spec[7:].strip().lower(), "domain")
    raise PatternError(f"{tool} does not take a parenthesised spec")


# ---------------------------------------------------------------- Bash


def _bash_segments(command: str) -> list[str] | None:
    """Whitespace-normalised segments, or None when no rule may ever match the command.

    The single place that decides matchability (also used by ``suggest``). Safe redirections are
    stripped first, then the RAW text is split (squashing first would erase newlines), then each
    segment is normalised.
    """
    if any(tok in command for tok in _UNSAFE_SUBSTITUTIONS):
        return None
    stripped = _SAFE_REDIRECT_RE.sub(" ", command)
    segs = [_squash(seg) for seg in _SEGMENT_SPLIT_RE.split(stripped)]
    if any(">" in seg or "&" in seg for seg in segs):
        return None
    return [seg for seg in segs if seg]


def _escape_glob(spec: str) -> str:
    return "".join({"[": "[[]", "]": "[]]", "?": "[?]"}.get(c, c) for c in spec)


def _segment_matches(p: Pattern, seg: str) -> bool:
    if p.tool != "Bash":
        return False
    if p.kind == "bare":
        return True
    spec = p.spec or ""
    if p.kind == "exact":
        return seg == spec
    if p.kind == "prefix":
        return seg == spec or seg.startswith(spec + " ")
    if p.kind == "glob":
        return fnmatch.fnmatchcase(seg, _escape_glob(spec))
    return False


# ---------------------------------------------------------------- paths


@lru_cache(maxsize=256)
def _path_glob_to_regex(spec: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(spec):
        if spec.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif spec.startswith("/**", i) and i + 3 == len(spec):
            out.append("/.*")
            i += 3
        elif spec.startswith("**", i):
            out.append(".*")
            i += 2
        elif spec[i] == "*":
            out.append("[^/]*")
            i += 1
        elif spec[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(spec[i]))
            i += 1
    return re.compile("".join(out))


def path_spec_matches(spec: str, rel: str) -> bool:
    """Does a path-rule glob ``spec`` cover the worktree-relative path ``rel``?"""
    return _path_glob_to_regex(spec).fullmatch(rel) is not None


@lru_cache(maxsize=None)
def _protected_regexes() -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(_path_glob_to_regex(g).pattern, re.IGNORECASE) for g in PROTECTED_PATHS)


def is_protected_path(rel: str) -> bool:
    # Case-insensitive: on case-insensitive filesystems ``.GIT`` is ``.git``.
    return any(rx.fullmatch(rel) for rx in _protected_regexes())


def _rel_path(input: dict, cwd: str | None) -> str | None:
    """Worktree-relative POSIX path, or None when absent or outside cwd."""
    raw = input.get("file_path") or input.get("notebook_path") or input.get("path")
    if not cwd or not raw or not isinstance(raw, str):
        return None
    base = os.path.realpath(cwd)
    path = os.path.realpath(os.path.join(base, raw))
    if os.path.commonpath([path, base]) != base or path == base:
        return None
    return os.path.relpath(path, base).replace(os.sep, "/")


def _tool_in_scope(p: Pattern, tool: str) -> bool:
    if p.tool == "Edit":
        return tool in FILE_TOOLS
    return tool == p.tool


# ---------------------------------------------------------------- matching


_PORT_RE = re.compile(r"[0-9]+")


def _hostname(input: dict) -> str | None:
    """Lowercased host of an http(s) URL, or None (unmatchable) when the URL is ambiguous.

    Python's ``urlsplit`` and the WHATWG parser the CLI uses disagree on backslashes,
    userinfo, stripped whitespace/control characters and odd ports (e.g.
    ``https://attacker.com\\@docs.python.org`` is docs.python.org here, attacker.com in Node),
    so any URL carrying one of those is refused rather than guessed at.
    """
    url = str(input.get("url", ""))
    if "\\" in url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or "@" in parts.netloc:
            return None
        if parts.username is not None or parts.password is not None:
            return None
        hostport = parts.netloc
        rest = hostport.rsplit("]", 1)[1] if hostport.startswith("[") else hostport
        if ":" in rest and not _PORT_RE.fullmatch(rest.rsplit(":", 1)[1]):
            return None
        host = parts.hostname
    except ValueError:
        return None
    return host.lower() if host else None


def _covers(p: Pattern, input: dict, cwd: str | None) -> bool:
    rel = _rel_path(input, cwd)
    if rel is None or is_protected_path(rel):
        return False
    return p.kind == "bare" or path_spec_matches(p.spec or "", rel)


def matches(p: Pattern, tool: str, input: dict, cwd: str | None) -> bool:
    """Single-rule check. For Bash every segment of the command must satisfy ``p``."""
    if tool in NEVER_RULED_TOOLS:
        return False
    if p.kind == "mcp":
        return tool == p.tool or tool.startswith(p.tool + "__")
    if p.kind == "domain":
        host = _hostname(input)
        dom = p.spec or ""
        return tool == p.tool and host is not None and (host == dom or host.endswith("." + dom))
    if p.tool == "Bash":
        if tool != "Bash":
            return False
        segs = _bash_segments(str(input.get("command", "")))
        return segs is not None and len(segs) > 0 and all(_segment_matches(p, s) for s in segs)
    if not _tool_in_scope(p, tool):
        return False
    if p.kind == "bare":
        return tool not in PATH_TOOLS or _covers(p, input, cwd)
    if p.kind == "path":
        return _covers(p, input, cwd)
    return False


def matches_any(
    patterns: Sequence[Pattern], tool: str, input: dict, cwd: str | None
) -> Pattern | None:
    """First pattern (sequence order) covering the request, else None.

    For Bash every segment must be covered by some pattern; the pattern returned is the one
    covering the first segment.
    """
    if tool != "Bash":
        return next((p for p in patterns if matches(p, tool, input, cwd)), None)
    segs = _bash_segments(str(input.get("command", "")))
    if not segs:
        return None
    first: Pattern | None = None
    for i, seg in enumerate(segs):
        cover = next((p for p in patterns if _segment_matches(p, seg)), None)
        if cover is None:
            return None
        if i == 0:
            first = cover
    return first


# ---------------------------------------------------------------- describe / suggest


def describe(pattern: str) -> str:
    p = parse(pattern)
    spec = p.spec or ""
    if p.kind == "mcp":
        if p.tool.count("__") >= 2:
            return f"The MCP tool {p.tool}"
        return f"Every tool on the MCP server {p.tool.split('__', 1)[1]}"
    if p.kind == "bare":
        return f"Every {p.tool} call"
    if p.kind == "exact":
        return f"Exactly the Bash command `{spec}`"
    if p.kind == "prefix":
        return f"Any Bash command that is `{spec}` or starts with `{spec} `"
    if p.kind == "glob":
        return f"Any Bash command matching `{spec}` (`*` = anything)"
    if p.kind == "domain":
        return f"Web fetches from {spec} and its subdomains"
    noun = "File reads" if p.tool == "Read" else "File edits/writes"
    if spec.endswith("/**"):
        return f"{noun} under {spec[:-2]} in the session's worktree"
    return f"{noun} matching {spec} in the session's worktree"


def suggest(tool: str, input: dict, cwd: str | None) -> list[str]:
    """Candidate rules, narrowest first; each is guaranteed to cover the request itself.

    Not filtered for safety: callers apply the safety assessor.
    """
    return [
        c
        for c in _suggest_candidates(tool, input, cwd)
        if matches_any([parse(c)], tool, input, cwd) is not None
    ]


def _suggest_candidates(tool: str, input: dict, cwd: str | None) -> list[str]:
    if tool in NEVER_RULED_TOOLS:
        return []
    if tool.startswith("mcp__"):
        return [tool, "mcp__" + tool.split("__")[1]]
    if tool == "Bash":
        command = str(input.get("command", ""))
        segs = _bash_segments(command)
        if not segs:
            return []
        cmd = _squash(_SAFE_REDIRECT_RE.sub(" ", command))
        words = segs[0].split()
        out = [f"Bash({cmd if len(segs) > 1 else segs[0]})"]
        # ``python -m <module>`` / ``uv run <command>``: the two-word prefix names no command.
        if len(words) >= 3 and (words[1] == "-m" or launcher_len(words) == 2):
            out.append(f"Bash({words[0]} {words[1]} {words[2]}:*)")
        if len(words) >= 2:
            out.append(f"Bash({words[0]} {words[1]}:*)")
        if all(seg.split()[0] == words[0] for seg in segs):
            out.append(f"Bash({words[0]}:*)")
        return list(dict.fromkeys(out))
    if tool in PATH_TOOLS:
        rel = _rel_path(input, cwd)
        if rel is None:
            return []
        head = "Read" if tool == "Read" else "Edit"
        dirs = [str(d) for d in PurePosixPath(rel).parents if str(d) != "."][:2]
        return [f"{head}({rel})", *(f"{head}({d}/**)" for d in dirs)]
    if tool == "WebFetch":
        host = _hostname(input)
        return [f"WebFetch(domain:{host})"] if host else []
    return [tool]
