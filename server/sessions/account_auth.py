"""Shared model-auth env builder for standard (non-Copilot) accounts.

Single source of truth for turning an account's stored setup-token into the env var the
Claude Agent SDK reads (``CLAUDE_CODE_OAUTH_TOKEN``). Session launch
(``server/sessions/orchestrator.py``'s ``_oat_env``) and the helper SDK factories
(``server/sessions/sdk_factory.py``'s ``real_suggestion_factory``/``real_summary_factory``) both
call this instead of each building their own copy — an account with a dead browser-OAuth refresh
token but a live setup-token must authenticate the same way everywhere.

Never logs the token value: ``crypto.read_oauth_token`` returns a secret.

``helper_env`` extends this to the nine LLM helper call sites (title/goal/open-question/
prompt-refiner/autopilot-decider/pr-intent/model-pick/backlog-grouping/nudge-composer) that have
only a ``CLAUDE_CONFIG_DIR``, not an account name (issue #751). It reverse-maps config_dir ->
account via ``adopt._map_account`` — the same reverse mapping process adoption already uses — and
is the ONLY place any of those nine sites builds its SDK env; a new call site must go through it
rather than reintroducing the bare ``{**os.environ, "CLAUDE_CONFIG_DIR": config_dir}`` construction
this module replaces.
"""
from __future__ import annotations

import logging
import os

from ccswitch import accounts as _accounts
from ccswitch import crypto
from ccswitch import paths as _paths
from server.sessions.adopt import _map_account

log = logging.getLogger(__name__)


def oauth_env_for(account: str) -> dict[str, str]:
    """Return ``{"CLAUDE_CODE_OAUTH_TOKEN": tok}`` for *account*'s setup-token, or ``{}``.

    Standard (non-Copilot) accounts only — Copilot accounts use a separate proxy-env path
    (``CopilotProxyManager.env_for``) that is out of scope here.
    """
    tok = crypto.read_oauth_token(account)
    return {"CLAUDE_CODE_OAUTH_TOKEN": tok} if tok else {}


def helper_env(config_dir: str) -> dict[str, str]:
    """Build the SDK env for a helper call site that has only a ``config_dir``.

    Returns ``{**os.environ, "CLAUDE_CONFIG_DIR": config_dir}`` merged with
    ``oauth_env_for(account)`` for the account *config_dir* reverse-maps to (via
    ``adopt._map_account``, matching by realpath equality against each known account's config
    dir) — so a helper call against an account whose browser-OAuth refresh is dead but whose
    setup-token still works authenticates the same way session launch does.

    Fail-open: an unmappable config_dir, a missing/unmapped account, or any lookup failure (e.g.
    a corrupt ledger) yields the base env unchanged. These are best-effort helper calls; a raise
    here must not crash an automation tick.

    No Copilot branch (unlike ``orchestrator._oat_env``, which routes Copilot accounts to
    ``CopilotProxyManager.env_for``): harmless today because ``oauth_env_for`` -> the reverse-
    mapped account's ``read_oauth_token`` returns ``None`` for Copilot accounts (they have no
    Anthropic setup-token), and ``pick_helper_config_dir`` already filters candidates to
    ``provider == "anthropic"`` before a config_dir ever reaches here — but a Copilot session's
    *own* config_dir does still reach ``title_generator``/``goal_inference`` via other paths, so
    a config_dir belonging to a Copilot account is not actually impossible here. Adding Copilot
    support to this function is out of scope for #751; if one of those two invariants ever
    changes, revisit.
    """
    env = {**os.environ, "CLAUDE_CONFIG_DIR": config_dir}
    try:
        known = [a["name"] for a in _accounts.list_accounts()]
        account = _map_account(config_dir, known, _paths.account_cfg_dir)
        if account:
            env.update(oauth_env_for(account))
    except Exception as exc:
        log.debug("helper_env: account lookup failed for config_dir=%s: %s", config_dir, exc)
        return env
    return env
