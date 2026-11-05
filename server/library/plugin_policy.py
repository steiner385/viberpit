"""Plugin marketplace policy (D-16c, owner decision D1 "curated only"): the ONE place that
decides whether a marketplace may be added and whether a plugin may be installed or updated.

Pure functions over data the caller loads (``scan.install_catalog``, the curated set), so the
rules are testable without a CLI and every route asks the same question. Everything fails
closed: an unknown marketplace, an unknown plugin, an unknown review state, a source that
might run a command — each is a refusal, never a pass.

Constants mirrored from the ``claude`` binary (2.1.318, docs/superpowers/specs/
2026-11-04-d16-verification.md §1.5) carry their offsets so the next CLI bump can re-check
them; the CLI reports neither the official set nor ``autoUpdate`` (probed: ``plugin
marketplace list --json`` and ``plugin list --json`` omit both), so they cannot be derived
from a CLI result.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

#: The built-in Anthropic Directory marketplace (``KQe`` @280773176). Never "added"; its
#: plugins are installable only when the directory cache marks them reviewed.
DIRECTORY_MARKETPLACE = "anthropic-plugin-directory"

#: ``Ggt`` @280773176: marketplaces the CLI auto-updates on startup by default...
OFFICIAL_MARKETPLACES = frozenset({
    "claude-code-marketplace", "claude-code-plugins", "claude-plugins-official",
    "anthropic-marketplace", "anthropic-plugins", "agent-skills", "anthropic-agent-skills",
    "life-sciences", "knowledge-work-plugins", "claude-for-legal",
    "claude-for-financial-services", "financial-services-plugins", "first-party-plugins",
    "claude-tag-plugins",
})
#: ...except these (``Fy`` @280773176).
_OFFICIAL_NO_AUTO_UPDATE = frozenset({"knowledge-work-plugins", "first-party-plugins"})
#: ``Nse`` @280773176: every name the CLI reserves for Anthropic (official, community,
#: directory). A third-party source that would claim one is an impersonation.
RESERVED_MARKETPLACE_NAMES = OFFICIAL_MARKETPLACES | frozenset({
    "claude-community", "claude-plugins-community", "healthcare",
    DIRECTORY_MARKETPLACE, "claude-plugin-directory",
})
#: The CLI's own impersonation pattern (@150894528), applied to the would-be name.
_IMPERSONATION_RE = re.compile(
    r"official[^a-z0-9]*(anthropic|claude)|(?:anthropic|claude)[^a-z0-9]*official")
#: The only GitHub owner that may publish a reserved name.
_OFFICIAL_OWNER = "anthropics"

# Refusal codes (constraints.md). HTTP status rides on the exception.
CONFIRM_REQUIRED = "PLUGIN_CONFIRM_REQUIRED"
MARKETPLACE_SOURCE_REFUSED = "PLUGIN_MARKETPLACE_SOURCE_REFUSED"
MARKETPLACE_NAME_RESERVED = "PLUGIN_MARKETPLACE_NAME_RESERVED"
MARKETPLACE_NOT_CURATED = "PLUGIN_MARKETPLACE_NOT_CURATED"
PLUGIN_NOT_FOUND = "PLUGIN_NOT_FOUND"
PLUGIN_UNREVIEWED = "PLUGIN_UNREVIEWED"
COMMAND_SOURCE_REFUSED = "PLUGIN_COMMAND_SOURCE_REFUSED"

_GITHUB_RE = re.compile(r"^(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]{1,100})$")
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))+$")
_URL_PATH_RE = re.compile(r"^(?:/[A-Za-z0-9._~-]+)+/?$")
_MAX_SOURCE = 300
MARKETPLACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class PolicyRefusal(Exception):
    """A D1 refusal: ``code`` is the wire code, ``http`` the status a route answers with."""

    def __init__(self, code: str, message: str, http: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http


@dataclass(frozen=True)
class InstallCatalog:
    """What the policy reads to judge an install: each curated marketplace's
    ``marketplace.json`` plugin entries (``{marketplace: {name: entry}}``; a marketplace
    whose file is unreadable is absent) and the Directory cache listings by entry name
    (``None`` when the cache is unreadable)."""

    marketplaces: Mapping[str, Mapping[str, dict[str, Any]]] = field(default_factory=dict)
    directory: Mapping[str, dict[str, Any]] | None = None


def valid_marketplace_name(name: str) -> bool:
    return bool(MARKETPLACE_NAME_RE.match(name))


def _would_be_name(repo: str) -> str:
    name = repo.rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".git") else name


def _guard_reserved(owner: str, repo: str) -> None:
    name = _would_be_name(repo)
    if (name in RESERVED_MARKETPLACE_NAMES or _IMPERSONATION_RE.search(name)) \
            and owner.lower() != _OFFICIAL_OWNER:
        raise PolicyRefusal(MARKETPLACE_NAME_RESERVED,
                            f"{name!r} is reserved for Anthropic's own marketplaces.", 400)


def check_marketplace_source(source: object) -> str:
    """The marketplace source an owner may add, normalized, or a ``PolicyRefusal``.

    Only a GitHub ``owner/repo`` or an ``https://`` git URL (no credentials, query,
    fragment, port-less IP or ``localhost``). Local paths, ``http://``, ``file:``, ssh and
    anything carrying whitespace or shell metacharacters are refused, as is a source whose
    name would impersonate an official marketplace from anyone but ``anthropics``."""
    def refuse(why: str) -> PolicyRefusal:
        return PolicyRefusal(MARKETPLACE_SOURCE_REFUSED,
                             f"Marketplace source refused: {why}. Use a GitHub owner/repo "
                             "or an https:// git URL.", 400)

    if not isinstance(source, str):
        raise refuse("not a string")
    if not source or len(source) > _MAX_SOURCE or source != source.strip():
        raise refuse("empty, too long or padded")
    if any(not c.isprintable() or c.isspace() for c in source):
        raise refuse("whitespace or control characters")
    if any(c in source for c in ";|&$`<>(){}[]*?!\\'\"#%^,=+") or ".." in source:
        raise refuse("shell metacharacters or a parent-path segment")
    if source.startswith("-"):
        raise refuse("looks like a flag")
    m = _GITHUB_RE.match(source)
    if m:
        _guard_reserved(m["owner"], m["repo"])
        return source
    if not source.startswith("https://"):
        raise refuse("not a GitHub owner/repo or an https:// URL")
    parts = urlsplit(source)
    host = parts.hostname or ""
    if (parts.username or parts.password or "@" in parts.netloc or parts.port is not None
            or parts.query or parts.fragment or not _HOST_RE.match(host)
            or host.lower() == "localhost" or host.replace(".", "").isdigit()
            or not _URL_PATH_RE.match(parts.path or "")):
        raise refuse("unsupported https URL")
    segments = [s for s in parts.path.split("/") if s]
    owner = segments[0] if host.lower() == "github.com" and len(segments) >= 2 else ""
    _guard_reserved(owner, segments[-1])
    return source


def is_command_source(entry: object) -> bool:
    """True when a catalog entry might run a marketplace-declared command at install: a
    ``command`` source, or a ``headersHelper`` anywhere in it. Walks the whole entry, so a
    nested or future placement is caught too (fail closed)."""
    if isinstance(entry, dict):
        if "headersHelper" in entry:
            return True
        src = entry.get("source")
        if isinstance(src, dict) and (src.get("source") == "command" or src.get("type") == "command"
                                      or "command" in src):
            return True
        return any(is_command_source(v) for v in entry.values())
    if isinstance(entry, list):
        return any(is_command_source(v) for v in entry)
    return False


def review_state(listing: Mapping[str, Any] | None) -> str | None:
    checks = listing.get("checks") if isinstance(listing, Mapping) else None
    review = checks.get("review") if isinstance(checks, Mapping) else None
    state = review.get("state") if isinstance(review, Mapping) else None
    return state if isinstance(state, str) else None


def is_reviewed(listing: Mapping[str, Any] | None) -> bool:
    """A Directory listing is reviewed ONLY when its state is exactly ``done``; ``none``,
    a missing block, or any state this code does not know is unreviewed."""
    return review_state(listing) == "done"


def check_install(plugin_id: str, curated: frozenset[str] | set[str], catalog: InstallCatalog) -> None:
    """THE install/update gate (D1). Returns only when *plugin_id* may be installed:

    * ``<name>@anthropic-plugin-directory`` whose Directory listing is reviewed (``done``);
    * ``<name>@<mkt>`` where *mkt* is curated (added by the owner through ViberPit, or
      configured on the host when D-16c shipped) and its catalog lists *name* with a
      source that runs no command.

    Anything else raises :class:`PolicyRefusal`, including a missing/unreadable catalog."""
    name, sep, marketplace = plugin_id.partition("@")
    if not sep or not name or not marketplace:
        raise PolicyRefusal(PLUGIN_NOT_FOUND, f"{plugin_id!r} is not <plugin>@<marketplace>.", 400)
    if marketplace == DIRECTORY_MARKETPLACE:
        listing = (catalog.directory or {}).get(name)
        if listing is None:
            raise PolicyRefusal(PLUGIN_NOT_FOUND,
                                f"{name!r} is not in the Anthropic Directory cache.", 404)
        if not is_reviewed(listing):
            raise PolicyRefusal(PLUGIN_UNREVIEWED,
                                f"{name!r} has not been reviewed by Anthropic, so it cannot be "
                                "installed from here.")
        if is_command_source(listing):
            raise _command_refusal(plugin_id)
        return
    if marketplace not in curated:
        raise PolicyRefusal(MARKETPLACE_NOT_CURATED,
                            f"Marketplace {marketplace!r} was not added in Settings, so its "
                            "plugins cannot be installed from here.")
    entry = (catalog.marketplaces.get(marketplace) or {}).get(name)
    if entry is None:
        raise PolicyRefusal(PLUGIN_NOT_FOUND,
                            f"{name!r} is not listed by marketplace {marketplace!r}.", 404)
    if is_command_source(entry):
        raise _command_refusal(plugin_id)


def _command_refusal(plugin_id: str) -> PolicyRefusal:
    return PolicyRefusal(COMMAND_SOURCE_REFUSED,
                         f"{plugin_id} installs by running a command the marketplace declares. "
                         "ViberPit never accepts those; install it in a terminal if you trust it.")


def auto_updates(name: str, config: Mapping[str, Any] | None) -> bool:
    """Whether the CLI auto-updates marketplace *name* on startup (D3): ``L2e``
    @280773176 — an explicit ``autoUpdate`` in ``known_marketplaces.json`` wins, else a
    ``claudeai``/``pluginDirectory`` source, else membership of the official set."""
    cfg = config if isinstance(config, Mapping) else {}
    explicit = cfg.get("autoUpdate")
    if isinstance(explicit, bool):
        return explicit
    src = cfg.get("source")
    if isinstance(src, Mapping) and src.get("source") in ("claudeai", "pluginDirectory"):
        return True
    if name == DIRECTORY_MARKETPLACE:  # built in: its source is pluginDirectory
        return True
    lowered = name.lower()
    return lowered in OFFICIAL_MARKETPLACES and lowered not in _OFFICIAL_NO_AUTO_UPDATE
