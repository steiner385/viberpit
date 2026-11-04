"""One truthful MCP status per server, derived from the shared grant (MCP auth overhaul, phase 1).

Pure functions, no IO. The shared store (``viberpit.creds.read_shared_mcp()``) is the one
credential authority, so whether a server is signed in is a fact about the grant, not about
any one account's probe or session. Per-account rows only add what the grant cannot know:
a configuration failure, or a live session the server rejected. Token values never leave
here."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from server.mcp_health.status import bundle_expiry, format_utc_ms, has_token
from viberpit.mcp_oauth_refresh import LEAD_S

Grant = Literal["never", "dead", "expired", "expiring", "live", "none"]

NOT_SIGNED_IN = "Not signed in — one sign-in covers every account and runner."
_REJECTED_PREFIX = "Sign-in rejected"
# Best state first: a server with several matching bundles takes the best of them.
_GRANT_RANK: tuple[Grant, ...] = ("live", "expiring", "expired", "never")
# Best row status first; ``needs-auth`` only counts when no grant exists to contradict it.
_ROW_RANK = ("connected", "pending", "needs-auth")
# Probes run only on demand, so this window bounds how long a stale row can pin a status.
FRESH_S = 24 * 3600


def store_key_name(name: str, plugin_name: str | None) -> str:
    """The ``serverName`` a bundle carries: plain for user servers, namespaced for plugins."""
    return name if plugin_name is None else f"plugin:{plugin_name}:{name}"


def _matching(store: dict, key_name: str, url: str | None) -> list[dict]:
    return [b for b in store.values()
            if isinstance(b, dict) and b.get("serverName") == key_name
            and (url is None or b.get("serverUrl") == url)]


def _bundle_state(bundle: dict, now_ms: int) -> Grant:
    exp = bundle_expiry(bundle)
    if not has_token(bundle) or (exp is not None and exp <= 0):
        return "never"                      # a cleared bundle
    if exp is None:
        return "live"                       # a token with no expiry never lapses
    if exp <= now_ms:
        return "expired"
    return "expiring" if exp <= now_ms + LEAD_S * 1000 else "live"


def grant_state(store: dict, *, key_name: str, url: str | None, now_ms: int) -> Grant:
    bundles = _matching(store, key_name, url)
    if not bundles:
        return "none"
    states = {_bundle_state(b, now_ms) for b in bundles}
    return next(g for g in _GRANT_RANK if g in states)


def best_expiry(store: dict, *, key_name: str, url: str | None) -> int | None:
    """The latest ``expiresAt`` (ms) among matching bundles that still hold a token."""
    exps = [int(exp) for b in _matching(store, key_name, url)
            if has_token(b) and (exp := bundle_expiry(b)) is not None and exp > 0]
    return max(exps) if exps else None


def _observed_ms(row: dict) -> float | None:
    raw = row.get("observedAt")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return None


def counted(row: dict, *, enabled_accounts: set[str], live_session_ids: set[str],
            now_ms: int) -> bool:
    """Whether a per-account row speaks for the server: its account is enabled, it was
    observed within ``FRESH_S``, and a session row's session is still live (a probe row
    has no session)."""
    if row.get("account") not in enabled_accounts:
        return False
    observed = _observed_ms(row)
    if observed is None or now_ms - observed > FRESH_S * 1000:
        return False
    return row.get("source") != "session" or row.get("sessionId") in live_session_ids


def _expired_copy(expires_at_ms: int | None) -> str:
    if expires_at_ms is None:
        return "Sign-in expired — background refresh is failing; sign in again."
    return f"Sign-in expired {format_utc_ms(expires_at_ms)} UTC — background refresh is failing; sign in again."


def _marker(error: str) -> str:
    start = error.find("(")
    end = error.find(")", start + 1)
    return error[start + 1:end] if start != -1 and end != -1 and end > start + 1 else "401"


def derive_overall(*, grant: Grant, rows: list[dict], now_ms: int,
                   expires_at_ms: int | None) -> dict:
    """``{"status", "error", "grant"}`` for one server; *rows* are already counted rows.
    ``status`` is None when nothing has checked the server yet."""
    def out(status: str | None, error: str | None = None) -> dict:
        return {"status": status, "error": error, "grant": grant}

    # 1. A configuration failure: signing in cannot fix it.
    for r in rows:
        if r.get("status") == "failed":
            return out("failed", r.get("error"))
    # 2. No usable grant.
    if grant in ("never", "dead"):
        return out("needs-auth", NOT_SIGNED_IN)
    if grant == "expired":
        return out("needs-auth", _expired_copy(expires_at_ms))
    # 3. A live session the server rejected despite a good-looking grant.
    if grant in ("live", "expiring"):
        for r in rows:
            err = r.get("error") or ""
            if (r.get("source") == "session" and r.get("status") == "needs-auth"
                    and err.startswith(_REJECTED_PREFIX)):
                return out("failed", f"Token rejected by the server while signed in ({_marker(err)}).")
    # 4. Best remaining row wins; probe needs-auth is noise whenever a grant exists.
    rest = [r for r in rows if grant == "none" or r.get("status") != "needs-auth"]
    for status in _ROW_RANK:
        if any(r.get("status") == status for r in rest):
            return out(status, next(r.get("error") for r in rest if r.get("status") == status))
    return out(rest[0].get("status"), rest[0].get("error")) if rest else out(None)
