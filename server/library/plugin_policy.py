"""Plugin marketplace policy (D-16c, owner decision D1 "curated only"): the ONE place that
decides whether a marketplace may be added or confirmed and whether a plugin may be
installed or updated.

Pure functions over data the caller loads (``scan.install_catalog``, the curated record),
so the edge and every host's daemon ask the same question. Everything fails closed: an
unknown marketplace, an unknown plugin, an unknown review state, a source that might run a
command, or a marketplace whose source no longer matches what the owner confirmed — each is
a refusal, never a pass.

**Curation is bound to a source, not a name.** The curated record maps a marketplace name
to the :func:`normalize_source` of the source the owner confirmed. A session can remove
``tools`` and re-add a different repo under the same name; the configured source then no
longer normalizes to the recorded one, and every check refuses with reason
``source_changed`` until the owner re-confirms.

Constants mirrored from the ``claude`` binary (2.1.318, docs/superpowers/specs/
2026-11-04-d16-verification.md §1.5) carry their offsets so the next CLI bump can re-check
them; the CLI reports neither the official set nor ``autoUpdate`` (probed: ``plugin
marketplace list --json`` and ``plugin list --json`` omit both), so they cannot be derived
from a CLI result.
"""
from __future__ import annotations

import ipaddress
import re
import socket
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
ENTRY_SOURCE_REFUSED = "PLUGIN_ENTRY_SOURCE_REFUSED"
DIRECTORY_UNAVAILABLE = "PLUGIN_DIRECTORY_UNAVAILABLE"

#: ``MARKETPLACE_NOT_CURATED`` reasons — the UI tells "confirm it" from "it changed".
REASON_NOT_CONFIRMED = "not_confirmed"
REASON_SOURCE_CHANGED = "source_changed"
#: ``PLUGIN_UNREVIEWED`` reason: the live Directory disagrees with the cached listing.
REASON_DIRECTORY_MISMATCH = "directory_mismatch"

# The CLI's own GitHub shorthand (``Lon`` @280765576); ``fullmatch`` everywhere (a ``$``
# under ``re.match`` also accepts a trailing newline).
_GITHUB_RE = re.compile(r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]{1,100})")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST_RE = re.compile(rf"{_LABEL}(?:\.{_LABEL})+")
_URL_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._~-]+)+/?")
# An IPv4 literal in any inet_aton spelling: every label decimal, octal or hex.
_NUMERIC_LABEL_RE = re.compile(r"(?:0[xX][0-9A-Fa-f]*|[0-9]+)")
_MAX_SOURCE = 300
MARKETPLACE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # the tailnet's range
#: Name suffixes that only resolve inside a private network (the tailnet's MagicDNS
#: ``*.ts.net``, mDNS, RFC 8375 and the conventional internal TLDs). Refused without DNS,
#: so the no-resolve plugin-entry check still stops a tailnet host.
_INTERNAL_SUFFIXES = (".ts.net", ".local", ".internal", ".intranet", ".lan", ".home.arpa",
                      ".corp", ".private")
#: A plugin-entry string the CLI could fetch from: a colon before any slash, which is
#: both a scheme URL (``https:``, ``file:``, ``git+ssh:``) and git's scp-like ssh form
#: (``[user@]host:path``). Relative paths and ``owner/repo`` carry neither.
_FETCH_TARGET_RE = re.compile(r"[^/]*:")


class PolicyRefusal(Exception):
    """A D1 refusal: ``code`` is the wire code, ``http`` the status a route answers with,
    ``reason`` a finer cause where the UI acts differently (``source_changed``)."""

    def __init__(self, code: str, message: str, http: int = 409, reason: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http
        self.reason = reason


@dataclass(frozen=True)
class InstallCatalog:
    """What the policy reads on ONE host: each configured marketplace's normalized source
    (``sources``; ``None`` when it does not normalize), each curated marketplace's
    ``marketplace.json`` entries (``{marketplace: {name: entry}}``; absent when unreadable),
    and the Directory cache by entry name (``None`` when unreadable)."""

    marketplaces: Mapping[str, Mapping[str, dict[str, Any]]] = field(default_factory=dict)
    directory: Mapping[str, dict[str, Any]] | None = None
    sources: Mapping[str, str | None] = field(default_factory=dict)


def valid_marketplace_name(name: object) -> bool:
    """The CLI accepts mixed case and is case-sensitive (probed), so case is kept."""
    return isinstance(name, str) and bool(MARKETPLACE_NAME_RE.fullmatch(name))


# --------------------------------------------------------------------------- normalizer

def _normalize_url(url: str) -> str | None:
    if not url.isascii():  # before any case-fold: see normalize_source
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment:
        return None
    host = parts.hostname.lower()
    path = parts.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    segments = [s for s in path.split("/") if s]
    if host in ("github.com", "www.github.com") and len(segments) == 2:
        return f"github:{segments[0]}/{segments[1]}".lower()
    port = f":{parts.port}" if parts.port is not None else ""
    return f"https:{host}{port}{path}"


def normalize_source(source: object) -> str | None:
    """THE marketplace-source identity, shared by add time (the owner's string) and check
    time (``known_marketplaces.json``'s source object as the CLI recorded it, ``bCe``
    @280765576). Two sources are the same marketplace iff they normalize equal:

    * ``owner/repo``, ``{"source": "github", "repo": …}`` and any ``https://github.com/o/r
      [.git]`` git/url form → ``github:o/r`` (lower-cased: GitHub names are case-blind);
    * another ``https`` URL, as ``git`` or ``url`` → ``https:<host><path>`` without a
      trailing ``/`` or ``.git``;
    * ``{"source": "directory", "path": P}`` → ``directory:P`` (exists so a host-side check
      can name it; the add and confirm policies refuse it);
    * a ``ref``/``path``/``sparsePaths`` pin, an ``http``/ssh/file URL, any non-ASCII
      string, or any other source type → ``None``, which never equals a recorded source
      (fail closed). Non-ASCII is refused BEFORE lower-casing: ``str.lower`` folds some
      non-ASCII letters into ASCII (KELVIN SIGN ``\u212a`` → ``k``), so the owner would
      approve one string while the CLI fetches another.
    """
    if isinstance(source, str):
        if not source.isascii():
            return None
        s = source.strip()
        if _GITHUB_RE.fullmatch(s):
            return f"github:{s}".lower()
        return _normalize_url(s) if s.startswith("https://") else None
    if not isinstance(source, Mapping):
        return None
    kind = source.get("source")
    if kind == "directory":
        path = source.get("path")
        return f"directory:{path}" if isinstance(path, str) and path.startswith("/") else None
    if any(k in source for k in ("ref", "path", "sparsePaths", "headers", "headersHelper")):
        return None
    if kind == "github":
        repo = source.get("repo")
        return f"github:{repo}".lower() if isinstance(repo, str) and repo.isascii() \
            and _GITHUB_RE.fullmatch(repo) else None
    if kind in ("git", "url"):
        url = source.get("url")
        return _normalize_url(url) if isinstance(url, str) else None
    return None


def add_form(normalized: str | None) -> str | None:
    """The ``marketplace add`` argument for a normalized source (``github:o/r`` → ``o/r``,
    ``https:h/p`` → ``https://h/p``), or ``None`` for one the add policy would refuse."""
    if not normalized:
        return None
    if normalized.startswith("github:"):
        return normalized[len("github:"):]
    if normalized.startswith("https:"):
        return "https://" + normalized[len("https:"):]
    return None


# --------------------------------------------------------------------------- add policy

def _resolve(host: str) -> list[str]:
    """The seam tests replace: every address *host* resolves to."""
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def _internal(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_unspecified or ip.is_reserved
            or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT))


def _check_host(host: str, *, resolve: bool) -> str | None:
    """Why *host* is refused, else ``None`` (see :func:`_host_verdict`)."""
    return _host_verdict(host, resolve=resolve)[0]


def _host_verdict(host: str, *, resolve: bool) -> tuple[str | None, list[str]]:
    """``(why, addresses)``: why *host* is refused, else ``None``, plus the addresses the
    check saw (empty unless *resolve* and accepted) so a caller can pin the clone to them.
    Refuses every IP-literal spelling
    (``127.1``, ``0x7f.1``, ``017700000001`` …), ``localhost`` and ``*.localhost``, and —
    a private-network suffix (``*.ts.net``, ``*.local`` … ``_INTERNAL_SUFFIXES``), and —
    when *resolve* — a name that resolves to a private, loopback, link-local, CGNAT
    (``100.64/10``, the tailnet), multicast, unspecified or reserved address, or does not
    resolve at all.

    The one DNS answer is also what :func:`resolved_pin` hands the clone, so the CLI's own
    ``git clone`` cannot re-resolve the name to something internal (DNS rebinding).
    """
    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return "localhost", []
    if lowered.endswith(_INTERNAL_SUFFIXES):
        return "a private-network name", []
    if all(_NUMERIC_LABEL_RE.fullmatch(label) for label in lowered.split(".")):
        return "an IP address", []
    try:
        ipaddress.ip_address(lowered)
        return "an IP address", []
    except ValueError:
        pass
    try:
        socket.inet_aton(lowered)
        return "an IP address", []
    except OSError:
        pass
    if not resolve:
        return None, []
    try:
        addrs = _resolve(lowered)
    except (OSError, UnicodeError):
        return "a host that does not resolve", []
    if not addrs:
        return "a host that does not resolve", []
    try:
        if any(_internal(a) for a in addrs):
            return "a host on a private or internal network", []
    except ValueError:
        return "a host with an unreadable address", []
    return None, addrs


def _would_be_name(repo: str) -> str:
    name = repo.rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".git") else name


def _guard_reserved(owner: str, repo: str) -> None:
    _guard_name(_would_be_name(repo), owner)


def _guard_name(name: str, owner: str) -> None:
    lowered = name.lower()
    if (lowered in RESERVED_MARKETPLACE_NAMES or _IMPERSONATION_RE.search(lowered)) \
            and owner.lower() != _OFFICIAL_OWNER:
        raise PolicyRefusal(MARKETPLACE_NAME_RESERVED,
                            f"{name!r} is reserved for Anthropic's own marketplaces.", 400)


def _source_owner(normalized: str) -> str:
    """The GitHub owner of a normalized source, else ``""`` (only GitHub can be
    ``anthropics``)."""
    if normalized.startswith("github:"):
        return normalized[len("github:"):].split("/", 1)[0]
    segments = normalized[len("https:"):].split("/") if normalized.startswith("https:") else []
    return segments[1] if len(segments) >= 3 and segments[0] == "github.com" else ""


def check_marketplace_name(name: str, normalized: str | None) -> None:
    """Refuse to curate a marketplace CONFIGURED under a reserved or official-looking
    name unless its source is owned by ``anthropics``. :func:`check_marketplace_source`
    guards the name a repo would get; this guards the name it actually has — a session can
    edit ``known_marketplaces.json`` so ``claude-plugins-official`` points at ``evil/x``,
    and the owner must never be asked to confirm that."""
    _guard_name(name, _source_owner(normalized or ""))


def check_marketplace_source(source: object, *, resolve: bool = True) -> str:
    """The marketplace source an owner may add, unchanged, or a ``PolicyRefusal``.

    Only a GitHub ``owner/repo`` or an ``https://`` git URL (no credentials, port, query
    or fragment; see :func:`_check_host` for the host rules). Local paths, ``http://``,
    ``file:``, ssh and anything carrying whitespace or shell metacharacters are refused, as
    is a source whose name would impersonate an official marketplace from anyone but
    ``anthropics``. *resolve* does a blocking DNS lookup: call it off the event loop."""
    return _checked_source(source, resolve=resolve)[0]


def _checked_source(source: object, *, resolve: bool) -> tuple[str, list[str]]:
    """:func:`check_marketplace_source` plus the addresses its one DNS lookup saw."""
    def refuse(why: str) -> PolicyRefusal:
        return PolicyRefusal(MARKETPLACE_SOURCE_REFUSED,
                             f"Marketplace source refused: {why}. Use a GitHub owner/repo "
                             "or an https:// git URL.", 400)

    if not isinstance(source, str):
        raise refuse("not a string")
    if not source or len(source) > _MAX_SOURCE or source != source.strip():
        raise refuse("empty, too long or padded")
    if not source.isascii():
        raise refuse("non-ASCII characters")
    if any(not c.isprintable() or c.isspace() for c in source):
        raise refuse("whitespace or control characters")
    if any(c in source for c in ";|&$`<>(){}[]*?!\\'\"#%^,=+") or ".." in source:
        raise refuse("shell metacharacters or a parent-path segment")
    if source.startswith("-"):
        raise refuse("looks like a flag")
    m = _GITHUB_RE.fullmatch(source)
    if m:
        _guard_reserved(m["owner"], m["repo"])
        return source, []
    if not source.startswith("https://"):
        raise refuse("not a GitHub owner/repo or an https:// URL")
    parts = urlsplit(source)
    host = parts.hostname or ""
    if (parts.username or parts.password or "@" in parts.netloc or parts.port is not None
            or parts.query or parts.fragment or not _HOST_RE.fullmatch(host)
            or not _URL_PATH_RE.fullmatch(parts.path or "")):
        raise refuse("unsupported https URL")
    why, addrs = _host_verdict(host, resolve=resolve)
    if why:
        raise refuse(f"it names {why}")
    segments = [s for s in parts.path.split("/") if s]
    owner = segments[0] if host.lower() == "github.com" and len(segments) >= 2 else ""
    _guard_reserved(owner, segments[-1])
    return source, addrs


def resolved_pin(source: str) -> str | None:
    """The libcurl ``CURLOPT_RESOLVE`` entry (``host:443:addr1,addr2``) that pins a clone of
    *source* to the addresses the SSRF check saw, or ``None`` for ``owner/repo`` (GitHub's
    own host, not a caller-chosen name). One DNS lookup serves the check and the pin.
    Raises the same :class:`PolicyRefusal` as :func:`check_marketplace_source`. Blocking."""
    checked, addrs = _checked_source(source, resolve=True)
    if not checked.startswith("https://"):
        return None
    host = (urlsplit(checked).hostname or "").lower().rstrip(".")
    return f"{host}:443:{','.join(sorted(set(addrs)))}"


def pin_for_normalized(normalized: str | None) -> str | None:
    """:func:`resolved_pin` for a normalized ``https:<host><path>`` source, else ``None``."""
    if not normalized or not normalized.startswith("https:"):
        return None
    return resolved_pin("https://" + normalized[len("https:"):])


# --------------------------------------------------------------------------- install policy

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


def _fetch_target_refusal(value: str) -> str | None:
    """Why the CLI must not fetch from *value*, else ``None``; a string that is not a fetch
    target (``./plugins/x``, ``owner/repo``, a package name) is ``None``."""
    if not _FETCH_TARGET_RE.match(value):
        return None
    if not value.isascii() or not value.startswith("https://"):
        return "not an https:// URL"
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 — raises on a malformed port
    except ValueError:
        return "an unreadable URL"
    host = parts.hostname or ""
    if parts.username or parts.password or "@" in parts.netloc or not _HOST_RE.fullmatch(host):
        return "an unsupported https URL"
    why = _check_host(host, resolve=False)
    return f"a URL naming {why}" if why else None


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def entry_source_refusal(entry: object) -> str | None:
    """THE SSRF rule for one catalog entry: why its ``source`` would make the CLI fetch
    from somewhere the add policy refuses, else ``None``. The marketplace add check covers
    the marketplace's own URL, but a confirmed marketplace's catalog may still list a plugin
    whose ``url``/``git``/registry source names an internal, ``http://``, ssh or tailnet
    address, which every host's CLI would fetch at install. Every string anywhere in
    ``source`` that looks like a fetch target must be a public-looking ``https://`` URL —
    the add rules without DNS (:func:`_check_host` with ``resolve=False``), so it is cheap
    enough for browse and the per-host re-check. Relative paths inside the marketplace and
    GitHub ``owner/repo`` shorthands pass."""
    src = entry.get("source") if isinstance(entry, Mapping) else None
    for value in _strings(src):
        why = _fetch_target_refusal(value.strip())
        if why:
            return why
    return None


def review_state(listing: Mapping[str, Any] | None) -> str | None:
    checks = listing.get("checks") if isinstance(listing, Mapping) else None
    review = checks.get("review") if isinstance(checks, Mapping) else None
    state = review.get("state") if isinstance(review, Mapping) else None
    return state if isinstance(state, str) else None


def is_reviewed(listing: Mapping[str, Any] | None) -> bool:
    """A Directory listing is reviewed ONLY when its state is exactly ``done``; ``none``,
    a missing block, or any state this code does not know is unreviewed."""
    return review_state(listing) == "done"


def _listing_repo(listing: Mapping[str, Any]) -> str | None:
    source = listing.get("source")
    repo = source.get("repository") if isinstance(source, Mapping) else None
    url = repo.get("url") if isinstance(repo, Mapping) else None
    return _normalize_url(url) if isinstance(url, str) else None


def check_directory_listing(cached: Mapping[str, Any], live: object) -> None:
    """The cache file is forgeable; the live Directory API is the authority. Passes only
    when *live* is the same listing (``id``), is reviewed, and names the same repository as
    the cached one (both absent passes on id + review alone; one-sided or unparseable is a
    mismatch)."""
    mismatch = PolicyRefusal(
        PLUGIN_UNREVIEWED,
        "The Anthropic Directory does not confirm this listing as reviewed and unchanged, "
        "so it cannot be installed from here.", reason=REASON_DIRECTORY_MISMATCH)
    if not isinstance(live, Mapping) or live.get("id") != cached.get("id") \
            or not isinstance(cached.get("id"), str) or not is_reviewed(live):
        raise mismatch
    if _listing_repo(live) != _listing_repo(cached):
        raise mismatch
    for side in (cached, live):  # a repository that is present but unparseable never matches
        source = side.get("source")
        has_repo = isinstance(source, Mapping) and source.get("repository") is not None
        if has_repo and _listing_repo(side) is None:
            raise mismatch


def check_curated(marketplace: str, curated: Mapping[str, str],
                  sources: Mapping[str, str | None]) -> None:
    """THE "is this marketplace curated, as configured right now?" check: the owner
    confirmed it, and its configured source still normalizes to the confirmed one."""
    recorded = curated.get(marketplace)
    if not recorded:
        raise PolicyRefusal(MARKETPLACE_NOT_CURATED,
                            f"Marketplace {marketplace!r} has not been confirmed in Settings, "
                            "so its plugins cannot be installed from here.",
                            reason=REASON_NOT_CONFIRMED)
    if sources.get(marketplace) != recorded:
        raise PolicyRefusal(MARKETPLACE_NOT_CURATED,
                            f"Marketplace {marketplace!r} now points at a different source than "
                            "the one confirmed — source changed, re-confirm in Settings.",
                            reason=REASON_SOURCE_CHANGED)


def check_install(plugin_id: str, curated: Mapping[str, str], catalog: InstallCatalog) -> None:
    """THE install/update gate (D1), run on the edge AND on every host against that host's
    own catalog. Returns only when *plugin_id* may be installed:

    * ``<name>@anthropic-plugin-directory`` whose Directory listing is reviewed (``done``);
    * ``<name>@<mkt>`` where *mkt* is curated (:func:`check_curated`: confirmed, and its
      source unchanged) and its catalog lists *name* with a source that runs no command
      and fetches from nowhere the add policy refuses (:func:`entry_source_refusal`).

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
    check_curated(marketplace, curated, catalog.sources)
    entry = (catalog.marketplaces.get(marketplace) or {}).get(name)
    if entry is None:
        raise PolicyRefusal(PLUGIN_NOT_FOUND,
                            f"{name!r} is not listed by marketplace {marketplace!r}.", 404)
    if is_command_source(entry):
        raise _command_refusal(plugin_id)
    why = entry_source_refusal(entry)
    if why:
        raise PolicyRefusal(ENTRY_SOURCE_REFUSED,
                            f"{plugin_id} would be fetched from {why}. ViberPit installs plugins "
                            "only from public https sources.")


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
