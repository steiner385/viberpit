"""Shared model-auth env builder for standard (non-Copilot) accounts.

Single source of truth for turning an account's stored setup-token into the env var the
Claude Agent SDK reads (``CLAUDE_CODE_OAUTH_TOKEN``). Session launch
(``server/sessions/orchestrator.py``'s ``_oat_env``) and the helper SDK factories
(``server/sessions/sdk_factory.py``'s ``real_suggestion_factory``/``real_summary_factory``) both
call this instead of each building their own copy — an account with a dead browser-OAuth refresh
token but a live setup-token must authenticate the same way everywhere.

Never logs the token value: ``crypto.read_oauth_token`` returns a secret.
"""
from __future__ import annotations

from ccswitch import crypto


def oauth_env_for(account: str) -> dict[str, str]:
    """Return ``{"CLAUDE_CODE_OAUTH_TOKEN": tok}`` for *account*'s setup-token, or ``{}``.

    Standard (non-Copilot) accounts only — Copilot accounts use a separate proxy-env path
    (``CopilotProxyManager.env_for``) that is out of scope here.
    """
    tok = crypto.read_oauth_token(account)
    return {"CLAUDE_CODE_OAUTH_TOKEN": tok} if tok else {}
