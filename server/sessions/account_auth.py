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

Because it is that single funnel, it is also where one-shot helpers are pointed at a HOST-LOCAL
scratch config dir instead of the shared account dir (``scratch_config.helper_config_dir``, #1005):
a helper that misreads a config file through a stale sshfs view must not be able to "repair" the
fleet's shared ``.claude.json`` by writing a default one back.

Live CODING sessions keep the real account dir — see ``scratch_config`` for why that asymmetry is
deliberate. The one live session that does not is the ASSISTANT (spec §4.1), which the orchestrator
points at ``helper_config_dir(..., isolated=True)`` directly (not through this function, since it
has an account name and needs no reverse mapping) to drop the operator's plugins/commands/settings.
"""
from __future__ import annotations

import logging
import os

from ccswitch import accounts as _accounts
from ccswitch import crypto
from ccswitch import paths as _paths
from server.sessions.adopt import _map_account
from server.sessions.scratch_config import helper_config_dir

log = logging.getLogger(__name__)


def oauth_env_for(account: str) -> dict[str, str]:
    """Return ``{"CLAUDE_CODE_OAUTH_TOKEN": tok}`` for *account*'s setup-token, or ``{}``.

    Standard (non-Copilot) accounts only — Copilot accounts use a separate proxy-env path
    (``CopilotProxyManager.env_for``) that is out of scope here.
    """
    tok = crypto.read_oauth_token(account)
    return {"CLAUDE_CODE_OAUTH_TOKEN": tok} if tok else {}


def helper_env(
    config_dir: str, account: str | None = None, *, writes_back: bool = False
) -> dict[str, str]:
    """Build the SDK env for a helper call site that has only a ``config_dir``.

    Returns ``{**os.environ, "CLAUDE_CONFIG_DIR": <scratch mirror of config_dir>}`` merged with
    ``oauth_env_for(account)`` for the account *config_dir* reverse-maps to (via
    ``adopt._map_account``, matching by realpath equality against each known account's config
    dir) — so a helper call against an account whose browser-OAuth refresh is dead but whose
    setup-token still works authenticates the same way session launch does. Pass *account*
    explicitly to skip the reverse mapping when the caller already knows it.

    The exported ``CLAUDE_CONFIG_DIR`` is ``scratch_config.helper_config_dir(config_dir)`` — a
    host-local copy, so a torn read of a shared config file can never be written back as a
    "repaired" default (#1005). The mapping is always done from the ORIGINAL *config_dir*: the
    scratch path is not an account dir and would not reverse-map.

    Pass ``writes_back=True`` for the rare call site whose spawn is expected to MUTATE the config
    dir and needs that mutation to persist for the account — ``claude mcp login``
    (``server/mcp_auth.py``) is the only one today: it stores the server's OAuth token in
    ``.credentials.json``, which a scratch copy would strand on this host. Stateless one-shot
    LLM helpers — the default — must never opt out.

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
    effective_dir = config_dir if writes_back else helper_config_dir(config_dir)
    env = {**os.environ, "CLAUDE_CONFIG_DIR": effective_dir}
    try:
        if account is None:
            known = [a["name"] for a in _accounts.list_accounts()]
            account = _map_account(config_dir, known, _paths.account_cfg_dir)
        if account:
            env.update(oauth_env_for(account))
    except Exception as exc:
        log.debug("helper_env: account lookup failed for config_dir=%s: %s", config_dir, exc)
        return env
    return env
