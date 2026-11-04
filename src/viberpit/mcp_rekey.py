"""The re-key plan: move each MCP OAuth grant to the key its server is configured under now.

The CLI files a grant under ``<serverName>|<hash(type, url, headers)>`` (``mcp_keys``), so
editing a server's URL or renaming it orphans the live grant under a key the CLI never looks
up again. :func:`plan_rekey` moves such a grant to the wanted key, but only when its grant
identity (``mcp_oauth_refresh.grant_identity``) equals the wanted server's and both URLs
share an origin; an unknown identity never moves. It then drops the only two kinds of
bundle ever deleted: one superseded by a configured server of the same name under another
key (if it holds a token, only once that is provably safe), and an unwanted one holding no
token at all.

Pure: no IO beyond the injected ``identity_of``, which is called only when a wanted server
lacks a usable grant, so a steady-state tick never touches the network. Token values are
never logged or returned anywhere but the plan's store.
"""
from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass

from .mcp_keys import prm_url_for, url_origin

IdentityOf = Callable[[str], "tuple[str, str] | None"]


@dataclass(frozen=True)
class WantedServer:
    """A configured server: its CLI key, the ``serverName`` its bundle carries, its URL."""
    key: str
    store_name: str
    url: str


@dataclass(frozen=True)
class RekeyPlan:
    """The store after the plan; ``moved`` maps old key → new key, ``dropped`` is deleted keys."""
    store: dict
    moved: dict[str, str]
    dropped: frozenset[str]


def _usable(bundle: object) -> bool:
    return isinstance(bundle, dict) and bool(bundle.get("refreshToken") or bundle.get("accessToken"))


def _prm_url(bundle: dict) -> str | None:
    ds = bundle.get("discoveryState")
    url = ds.get("resourceMetadataUrl") if isinstance(ds, dict) else None
    return url if isinstance(url, str) and url else None


def _expires_at(bundle: dict) -> float:
    value = bundle.get("expiresAt")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _same_origin(a: object, b: str) -> bool:
    origin = url_origin(a) if isinstance(a, str) else None
    return origin is not None and origin == url_origin(b)


def plan_rekey(store: dict, wanted: list[WantedServer], identity_of: IdentityOf) -> RekeyPlan:
    """Plan moving grants onto their servers' current keys, then dropping dead bundles.

    A wanted server without a usable grant (no ``refreshToken``, empty ``accessToken``) takes
    a donor: an unwanted bundle with a ``refreshToken`` and a ``resourceMetadataUrl``, whose
    ``serverUrl`` shares the wanted URL's origin and whose identity equals the wanted
    server's. Among several, one already named for the server wins, then the latest
    ``expiresAt``. Drop rules run on the post-move store: an empty bundle always goes; a
    superseded one holding a token goes only when its server's key now holds a usable grant
    or both identities are known and differ. *store* is not mutated."""
    out = copy.deepcopy(store)
    wanted_keys = {w.key for w in wanted}
    moved: dict[str, str] = {}
    identities: dict[str, tuple[str, str] | None] = {}

    def identity(prm: str | None) -> tuple[str, str] | None:
        if not prm:
            return None
        if prm not in identities:
            identities[prm] = identity_of(prm)
        return identities[prm]

    for w in wanted:
        if _usable(out.get(w.key)):
            continue
        donors = [key for key, bundle in out.items()
                  if key not in wanted_keys and isinstance(bundle, dict)
                  and bundle.get("refreshToken") and _prm_url(bundle)
                  and _same_origin(bundle.get("serverUrl"), w.url)]
        if not donors:
            continue
        target = identity(prm_url_for(w.url))
        if target is None:
            continue
        matching = [key for key in donors if identity(_prm_url(out[key])) == target]
        if not matching:
            continue
        donor = max(matching, key=lambda k: (out[k].get("serverName") == w.store_name,
                                             _expires_at(out[k])))
        bundle = out.pop(donor)
        bundle["serverName"] = w.store_name
        bundle["serverUrl"] = w.url
        out[w.key] = bundle  # replaces at most an unusable (token-less) bundle
        moved[donor] = w.key

    by_name = {w.store_name: w for w in wanted}
    dropped = set()
    for key, bundle in list(out.items()):
        if key in wanted_keys:
            continue
        # A non-dict value under an unwanted key holds no token, so it counts as empty.
        if not _usable(bundle):  # (b) empty, or (a) superseded with no token
            drop = True
        else:
            owner = by_name.get(bundle.get("serverName"))
            if owner is None:
                drop = False  # unconfigured, token-holding: maybe a repo .mcp.json server
            elif _usable(out.get(owner.key)):
                drop = True  # (a) superseded by a live grant
            else:
                own = _prm_url(bundle)
                theirs = identity(prm_url_for(owner.url)) if own else None
                mine = identity(own) if theirs is not None else None
                drop = theirs is not None and mine is not None and theirs != mine
        if drop:
            del out[key]
            dropped.add(key)
    return RekeyPlan(store=out, moved=moved, dropped=frozenset(dropped))
