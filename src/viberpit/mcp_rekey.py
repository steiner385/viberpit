"""The re-key plan: move each MCP OAuth grant to the key its server is configured under now.

The CLI files a grant under ``<serverName>|<hash(type, url, headers)>`` (``mcp_keys``), so
editing a server's URL or renaming it orphans the live grant under a key the CLI never looks
up again. :func:`plan_rekey` moves such a grant to the wanted key, but only when its grant
identity (``mcp_oauth_refresh.grant_identity``) equals the wanted server's; an unknown
identity never moves. It then drops the only two kinds of bundle ever deleted: one
superseded by a configured server of the same name under another key, and an unwanted one
holding no token at all.

Pure: no IO beyond the injected ``identity_of``, which is called only when a wanted server
lacks a usable grant, so a steady-state tick never touches the network. Token values are
never logged or returned anywhere but the plan's store.
"""
from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass

from .mcp_keys import prm_url_for

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


def _donors(store: dict, wanted_keys: set[str]) -> list[str]:
    return [key for key, bundle in store.items()
            if key not in wanted_keys and isinstance(bundle, dict)
            and bundle.get("refreshToken") and _prm_url(bundle)]


def plan_rekey(store: dict, wanted: list[WantedServer], identity_of: IdentityOf) -> RekeyPlan:
    """Plan moving grants onto their servers' current keys, then dropping dead bundles.

    A wanted server without a usable grant (no ``refreshToken``, empty ``accessToken``) takes
    the donor (an unwanted bundle with a ``refreshToken`` and a ``resourceMetadataUrl``) whose
    identity equals its own, latest ``expiresAt`` first. Drop rules run on the post-move
    store. *store* is not mutated."""
    out = copy.deepcopy(store)
    wanted_keys = {w.key for w in wanted}
    moved: dict[str, str] = {}
    donor_identity: dict[str, tuple[str, str] | None] = {}

    for w in wanted:
        if _usable(out.get(w.key)):
            continue
        donors = _donors(out, wanted_keys)
        if not donors:
            continue
        prm = prm_url_for(w.url)
        target = identity_of(prm) if prm else None
        if target is None:
            continue
        matching = []
        for key in donors:
            if key not in donor_identity:
                donor_prm = _prm_url(out[key])
                donor_identity[key] = identity_of(donor_prm) if donor_prm else None
            if donor_identity[key] == target:
                matching.append(key)
        if not matching:
            continue
        donor = max(matching, key=lambda k: _expires_at(out[k]))
        bundle = out.pop(donor)
        bundle["serverName"] = w.store_name
        bundle["serverUrl"] = w.url
        out[w.key] = bundle
        moved[donor] = w.key

    wanted_names = {w.store_name for w in wanted}
    dropped = set()
    for key, bundle in list(out.items()):
        if key in wanted_keys:
            continue
        name = bundle.get("serverName") if isinstance(bundle, dict) else None
        if name in wanted_names or not _usable(bundle):  # (a) superseded, (b) empty
            del out[key]
            dropped.add(key)
    return RekeyPlan(store=out, moved=moved, dropped=frozenset(dropped))
