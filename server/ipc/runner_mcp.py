"""The runner's half of MCP grant distribution (#3287; spec 2026-11-04 §3 runners, §6 runner
capture).

The edge pushes the shared MCP store's public view beside the Claude envelopes on every
``creds_sync``; :mod:`server.ipc.creds_rpc` lands it in :func:`viberpit.mcp_view_store.runner_view`
and then calls :func:`sync_local_dirs` here. This module decides whether the runner may write
its own account dirs at all and, when it may, publishes the view into each of them the way the
edge does for its own tree (``creds.sync_cfg_mcp_oauth``, which on a worker reads the pushed view)
and returns the dirs' refresh-token-bearing bundles — terminal ``claude mcp login``s made on this
host — as the ``mcp_offers`` the edge merges under its store lock.

Designer call 1: while the runner's account tree is still the sshfs mount of the edge's
``~/.claude-accounts`` the runner only caches the view. The edge's own publish already covers a
shared tree, and two writers of one dir over a network mount is the race this phase exists to
retire. :func:`owns_account_dirs` therefore requires ``cfg_root()`` to be *provably* local
(:func:`server.obs.logdir.is_shared_dir` fails closed on an unreadable mount table).

Everything here blocks on dir and mount-table IO; the RPC handler threads it (#2852).
"""
from __future__ import annotations

import logging

from server.fsmounts import Mounts
from server.obs.logdir import is_shared_dir
from viberpit import creds, fleet_role, paths
from viberpit.configdir import CREDENTIALS_NAME
from viberpit.mcp_view_store import runner_view

log = logging.getLogger(__name__)


def owns_account_dirs(*, mounts: Mounts | None = None) -> bool:
    """True when this daemon may write MCP views into its account dirs: a worker whose
    ``cfg_root()`` is provably on a host-local filesystem."""
    return fleet_role.is_worker() and not is_shared_dir(paths.cfg_root(), mounts=mounts)


def sync_local_dirs(*, mounts: Mounts | None = None) -> dict:
    """Publish the pushed view into every local account dir; return the dirs' offers.

    ``{}`` without touching the filesystem (beyond the mount table) when the view has never
    synced or :func:`owns_account_dirs` is False. Otherwise, for every child dir of
    ``cfg_root()`` holding ``CREDENTIALS_NAME``: its offers under :func:`creds.offerable_mcp`
    are read *before* the publish, folded with :func:`creds.merge_mcp_bundles`, then
    :func:`creds.sync_cfg_mcp_oauth` lands the view — minus the keys the dir is still ahead of
    (:func:`creds.publishable_store`), so a login is stripped only once a pushed view holds it. A dir that fails is skipped (DEBUG with traceback) so one torn file never holds the
    others back. Never logs token values: counts only."""
    view = runner_view()
    store = view.bundles()
    if store is None or not owns_account_dirs(mounts=mounts):
        return {}
    drop = creds.pushed_drop_keys()
    offers: dict = {}
    written = 0
    root = paths.cfg_root()
    for cfg_dir in sorted(root.iterdir()) if root.is_dir() else []:
        cred = cfg_dir / CREDENTIALS_NAME
        if not (cfg_dir.is_dir() and cred.is_file()):
            continue
        try:
            dir_mcp = creds.read_json(cred).get("mcpOAuth")
            dir_mcp = dir_mcp if isinstance(dir_mcp, dict) else {}
            offers = creds.merge_mcp_bundles(offers, creds.offerable_mcp(dir_mcp, store, drop=drop))
            # The publish leaves a dir copy the view has not caught up with alone
            # (creds.publishable_store), so a login is stripped only once the edge holds it.
            if creds.sync_cfg_mcp_oauth(cfg_dir, drop=drop):
                written += 1
        except Exception:
            log.debug("runner mcp view: skipping account dir %s", cfg_dir.name, exc_info=True)
    if written or offers:
        log.info("runner mcp view published dirs=%d offered=%d", written, len(offers))
    return offers
