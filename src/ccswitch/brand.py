"""Brand identity — the ONE place the product name is spelled.

Display strings, User-Agents, notification titles, and filenames derive from
``NAME`` / ``SLUG``. Wire contracts that cannot change without a dual-accept
window are pinned to ``LEGACY_SLUG`` here and flip in their own phase (see
docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md §3). Nothing outside
this module may spell the brand.
"""
from __future__ import annotations

import re

#: Display name — UI, page titles, prose, push-notification titles, GitHub comments.
NAME = "ViberPit"
#: Lowercase identifier stem — User-Agent product, download filenames, new slugs.
SLUG = "viberpit"
#: The pre-rebrand slug. Only compat readers (parsers, path fallbacks) may use it.
LEGACY_SLUG = "ccswitch"

#: Environment-variable prefix. Both prefixes are live for the whole rebrand:
#: ``ccswitch.env_compat.mirror_legacy_env()`` fills each from the other at process
#: start, so an ``/etc`` env file may spell either one. The reverse direction retires
#: in Phase 7, once every host's env file is confirmed migrated.
ENV_PREFIX = f"{SLUG.upper()}_"
#: The pre-rebrand environment-variable prefix, still read everywhere.
LEGACY_ENV_PREFIX = f"{LEGACY_SLUG.upper()}_"

#: The installed console script. CLI message prefixes name the binary the user ran,
#: so this flips in Phase 5 when the ``viberpit`` entry point is added.
CLI_NAME = LEGACY_SLUG
#: Stem of /etc, /run, /var/lib, /var/log paths and systemd units. Flips in Phase 3
#: together with the unit files and the per-host migration runbook.
HOST_SLUG = SLUG
#: MCP server wire name. The SDK derives tool names ``mcp__<name>__<tool>`` from it,
#: and the permission registry string-matches the prefix. Flipped in Phase 2a
#: together with the accept-both helpers below — never on its own.
MCP_SERVER_NAME = SLUG
MCP_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
#: The pre-rebrand MCP namespace. Still matched by every reader, permanently:
#: a stored transcript is re-parsed on replay, and a Codex thread resumed across
#: the flip keeps the names it was started with, so a name written before the
#: flip must keep resolving to its annotation forever. NOT Phase-7 scaffolding.
LEGACY_MCP_SERVER_NAME = LEGACY_SLUG
LEGACY_MCP_TOOL_PREFIX = f"mcp__{LEGACY_MCP_SERVER_NAME}__"
#: Emitted prefix first, so a caller that wants "the" prefix reads ``[0]``.
MCP_TOOL_PREFIXES = (MCP_TOOL_PREFIX, LEGACY_MCP_TOOL_PREFIX)


def mcp_tool_stem(name: str) -> str | None:
    """The bare tool name if *name* is one of ours under EITHER prefix, else ``None``.

    The one place the dual-accept is spelled. Every reader that keys on a bare
    tool name (``assistant.tools.annotation_for``, the permission registry's
    app-action branch, the web transcript presenter) goes through this rather
    than growing its own ``startswith`` tuple — those readers fail closed, so a
    copy that missed a prefix would silently downgrade a destructive tool's card
    to the generic ``mcp__`` auto-allow.
    """
    if not isinstance(name, str):
        return None
    for prefix in MCP_TOOL_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return None


def is_mcp_tool(name: str) -> bool:
    """Whether *name* is one of our own MCP tools under either prefix."""
    return mcp_tool_stem(name) is not None


#: Prefix of the per-session child branch a worktree is created on
#: (``viberpit/<base>-<short-id>``). EMITTED form — one spelling, like the markers.
BRANCH_PREFIX = f"{SLUG}/"
#: The pre-rebrand branch prefix. Matched permanently, and for a harder reason than
#: the markers: a branch is not ours to rewrite. Pushed copies live on remotes (and
#: in other people's clones) indefinitely, a session resumed across the flip is still
#: sitting on the branch it was created on, and the one consumer that matches this
#: prefix is a ``git branch -D``. A reader that lost the legacy spelling would simply
#: stop tidying every pre-flip branch, silently and forever.
LEGACY_BRANCH_PREFIX = f"{LEGACY_SLUG}/"
#: Emitted prefix first, so a caller that wants "the" prefix reads ``[0]``.
BRANCH_PREFIXES = (BRANCH_PREFIX, LEGACY_BRANCH_PREFIX)


def is_session_branch(branch: str) -> bool:
    """Whether *branch* is a per-session child branch we created, either spelling.

    The one place the branch dual-accept is spelled, for the same reason as
    :func:`mcp_tool_stem`: the caller (``worktrees.remove_worktree``) deletes the
    branch on this answer, so a second hand-typed ``startswith`` that missed a
    prefix would leak one brand's branches into the repo forever while looking
    exactly like the working code next to it.
    """
    if not isinstance(branch, str):
        return False
    return branch.startswith(BRANCH_PREFIXES)


#: Per-repo namespace dot-dir under a repo root (clones, worktrees, scratch). EMITTED
#: form, for NEW repo roots only: ``paths.repo_namespace`` keeps a root that already
#: has only the legacy dir on it forever, because a live worktree holds an absolute
#: ``gitdir:`` pointer and a ``Session.cwd`` row into that tree — moving it strands
#: the session. So both spellings are matched permanently, not Phase-7 scaffolding.
REPO_NAMESPACE = f".{SLUG}"
LEGACY_REPO_NAMESPACE = f".{LEGACY_SLUG}"
#: Emitted spelling first, so a caller that wants "the" name reads ``[0]``.
REPO_NAMESPACES = (REPO_NAMESPACE, LEGACY_REPO_NAMESPACE)
#: Workspace-local Codex attachment staging dir — same new-dirs-only rule, resolved
#: per workspace by ``paths.codex_attachment_staging_dirname``.
CODEX_ATTACHMENTS_DIRNAME = f".{SLUG}-codex-attachments"
LEGACY_CODEX_ATTACHMENTS_DIRNAME = f".{LEGACY_SLUG}-codex-attachments"
CODEX_ATTACHMENTS_DIRNAMES = (CODEX_ATTACHMENTS_DIRNAME, LEGACY_CODEX_ATTACHMENTS_DIRNAME)


def in_repo_namespace(parts: tuple[str, ...]) -> bool:
    """Whether a ``Path.parts`` tuple has a repo-namespace segment, either spelling.

    The one place the segment dual-accept is spelled. Every containment guard of
    the shape "is this path under our dot-dir?" goes through here: the purge guard
    that answers it deletes a directory on ``True`` and leaves it alone on
    ``False``, so a copy that knew one spelling would silently stop cleaning (or
    start refusing) every directory of the other.
    """
    return any(ns in parts for ns in REPO_NAMESPACES)


#: Regex fragment matching either brand spelling. For compat PARSERS only —
#: never for emission. Permanent, for the same reason as the legacy MCP prefix:
#: markers and context headers live in stored transcripts that are re-parsed on
#: every replay, so text written before the flip must parse indefinitely.
EITHER_SLUG_RE = f"(?:{SLUG}|{LEGACY_SLUG})"


def marker(name: str) -> str:
    """The emitted HTML-comment marker for *name* (e.g. ``complete`` → ``<!-- viberpit:complete -->``).

    Emission is single-spelled on purpose: an agent is taught exactly one form.
    Pair every call with :func:`marker_re` on the parsing side.
    """
    return f"<!-- {SLUG}:{name} -->"


def marker_re(name: str) -> re.Pattern[str]:
    """Compiled matcher for :func:`marker`'s output in EITHER brand spelling.

    Whitespace inside the comment is tolerated because the marker is reproduced
    by a language model, not by code — an agent that writes ``<!--viberpit:complete-->``
    meant the marker, and a session that never resolves is a far worse failure
    than a slightly lenient match.
    """
    return re.compile(rf"<!--\s*{EITHER_SLUG_RE}:{re.escape(name)}\s*-->")
