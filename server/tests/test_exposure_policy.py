"""The exposure policy (#1410, FR-G12.1/.2/.4, AC-G12.1/.2/.9): table-driven over tiers.

Each tier's satisfied configuration starts; removing any one prerequisite refuses with
a ``RuntimeError`` naming the tier and the rule. ``VIBERPIT_DEV=1`` downgrades only
loopback/tailnet refusals.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from server.auth import exposure

_ALL_KEYS = ("VIBERPIT_EXPOSURE", "VIBERPIT_HOST", "VIBERPIT_TAILNET_ALLOWLIST",
             "VIBERPIT_PUBLIC_ORIGIN", "VIBERPIT_ALLOWED_ORIGINS", "VIBERPIT_WEBAUTHN_ENABLED",
             "VIBERPIT_SESSION_SECRET", "VIBERPIT_MASTER_KEY", "VIBERPIT_DEV",
             "VIBERPIT_DEV_INSECURE_COOKIE", "VIBERPIT_INGRESS_HSTS")

_OK: dict[str, dict[str, str]] = {
    "loopback": {"VIBERPIT_HOST": "127.0.0.1"},
    "tailnet": {"VIBERPIT_HOST": "127.0.0.1", "VIBERPIT_TAILNET_ALLOWLIST": "me@example.com",
                "VIBERPIT_PUBLIC_ORIGIN": "https://edge.example.test"},
    "lan": {"VIBERPIT_HOST": "192.168.1.10", "VIBERPIT_TAILNET_ALLOWLIST": "me@example.com",
            "VIBERPIT_ALLOWED_ORIGINS": "https://edge.lan.example.test",
            "VIBERPIT_WEBAUTHN_ENABLED": "true", "VIBERPIT_MASTER_KEY": "k" * 32},
    "public": {"VIBERPIT_HOST": "0.0.0.0", "VIBERPIT_TAILNET_ALLOWLIST": "me@example.com",
               "VIBERPIT_PUBLIC_ORIGIN": "https://edge.example.test",
               "VIBERPIT_WEBAUTHN_ENABLED": "true", "VIBERPIT_SESSION_SECRET": "s" * 32,
               "VIBERPIT_INGRESS_HSTS": "true"},
}

# (tier, env override, rule fragment the refusal must name)
_REFUSALS = [
    ("loopback", {"VIBERPIT_HOST": "0.0.0.0"}, "not a loopback address"),
    ("loopback", {"VIBERPIT_HOST": "192.168.1.10"}, "not a loopback address"),
    ("tailnet", {"VIBERPIT_HOST": "0.0.0.0"}, "neither loopback nor a tailnet address"),
    ("tailnet", {"VIBERPIT_HOST": "192.168.1.10"}, "neither loopback nor a tailnet address"),
    ("tailnet", {"VIBERPIT_TAILNET_ALLOWLIST": ""}, "empty tailnet allowlist"),
    ("tailnet", {"VIBERPIT_PUBLIC_ORIGIN": ""}, "no configured browser origin"),
    ("tailnet", {"VIBERPIT_WEBAUTHN_ENABLED": "true", "VIBERPIT_PUBLIC_ORIGIN": ""},
     "no configured browser origin"),
    ("lan", {"VIBERPIT_HOST": "8.8.8.8"}, "not a private address"),
    ("lan", {"VIBERPIT_HOST": "0.0.0.0"}, "not a private address"),
    ("lan", {"VIBERPIT_TAILNET_ALLOWLIST": ""}, "empty tailnet allowlist"),
    ("lan", {"VIBERPIT_ALLOWED_ORIGINS": ""}, "no configured browser origin"),
    ("lan", {"VIBERPIT_WEBAUTHN_ENABLED": "false"}, "passkeys are disabled"),
    ("lan", {"VIBERPIT_MASTER_KEY": ""}, "no persistent cookie-signing secret"),
    ("lan", {"VIBERPIT_DEV_INSECURE_COOKIE": "1"}, "VIBERPIT_DEV_INSECURE_COOKIE"),
    ("public", {"VIBERPIT_TAILNET_ALLOWLIST": ""}, "empty tailnet allowlist"),
    ("public", {"VIBERPIT_WEBAUTHN_ENABLED": "false"}, "passkeys are disabled"),
    ("public", {"VIBERPIT_SESSION_SECRET": "", "VIBERPIT_MASTER_KEY": "k" * 32},
     "VIBERPIT_SESSION_SECRET is not set"),
    ("public", {"VIBERPIT_PUBLIC_ORIGIN": "", "VIBERPIT_ALLOWED_ORIGINS":
                "https://edge.example.test"}, "no https VIBERPIT_PUBLIC_ORIGIN"),
    ("public", {"VIBERPIT_INGRESS_HSTS": ""}, "HSTS at the ingress is not attested"),
    ("public", {"VIBERPIT_DEV_INSECURE_COOKIE": "1"}, "VIBERPIT_DEV_INSECURE_COOKIE"),
]


def _env(tier: str, override: dict[str, str] | None = None, *, dev: bool = False):
    env = {k: v for k, v in os.environ.items() if k not in _ALL_KEYS}
    env.update({"VIBERPIT_EXPOSURE": tier, **_OK.get(tier, {}), **(override or {})})
    env = {k: v for k, v in env.items() if v != ""}
    if dev:
        env["VIBERPIT_DEV"] = "1"
    return patch.dict(os.environ, env, clear=True)


@pytest.fixture(autouse=True)
def _fresh_warnings():
    exposure._warned_dev.clear()


@pytest.mark.parametrize("tier", list(_OK))
def test_each_satisfied_tier_starts(tier):
    with _env(tier):
        assert exposure.exposure_violations() == []
        exposure.validate_exposure()


@pytest.mark.parametrize("tier,override,rule", _REFUSALS)
def test_exposure_matrix_refuses_with_tier_and_rule(tier, override, rule):
    with _env(tier, override):
        with pytest.raises(RuntimeError) as exc:
            exposure.validate_exposure()
    message = str(exc.value)
    assert f"exposure={tier}" in message and rule in message and "fix:" in message


def test_unknown_tier_refuses_even_in_dev():
    with _env("wide-open", dev=True):
        with pytest.raises(RuntimeError, match="unknown tier 'wide-open'"):
            exposure.validate_exposure()


def test_default_tier_is_tailnet():
    with patch.dict(os.environ, {}, clear=True):
        assert exposure.tier() == "tailnet"


@pytest.mark.parametrize("tier,override,rule",
                         [r for r in _REFUSALS if r[0] in ("loopback", "tailnet")])
def test_dev_relaxes_loopback_and_tailnet_to_one_warning(tier, override, rule, caplog):
    with _env(tier, override, dev=True):
        exposure.validate_exposure()
        exposure.validate_exposure()
    warnings = [r for r in caplog.records if "SECURITY" in r.getMessage()]
    assert len(warnings) == 1 and rule in warnings[0].getMessage()


@pytest.mark.parametrize("tier,override,rule",
                         [r for r in _REFUSALS if r[0] in ("lan", "public")])
def test_dev_never_relaxes_lan_or_public(tier, override, rule):
    with _env(tier, override, dev=True):
        with pytest.raises(RuntimeError, match=f"exposure={tier}"):
            exposure.validate_exposure()


def test_lan_and_public_require_persistent_secret():
    """AC-G12.9: _signing_secret's ephemeral per-process fallback is a refusal there."""
    for tier in ("lan", "public"):
        with _env(tier, {"VIBERPIT_SESSION_SECRET": "", "VIBERPIT_MASTER_KEY": ""}):
            with pytest.raises(RuntimeError, match="persistent cookie-signing secret"):
                exposure.validate_exposure()


def test_tailnet_keeps_one_shot_ephemeral_secret_warning():
    with _env("tailnet", {"VIBERPIT_SESSION_SECRET": "", "VIBERPIT_MASTER_KEY": ""}):
        exposure.validate_exposure()     # no refusal: tailnet re-verifies identity per request


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["lan", "public"])
async def test_passkey_tier_needs_a_registered_credential(tier, tmp_path):
    from server.db import db_session, init_db
    from server.db.clock import naive_utc_now
    from server.db.models import WebAuthnCredential
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'x.db'}")
    with _env(tier):
        async with db_session() as db:
            with pytest.raises(RuntimeError, match=f"exposure={tier}: no registered passkey"):
                await exposure.validate_exposure_credentials(db)
            db.add(WebAuthnCredential(identity="me@example.com", credential_id="c",
                                      public_key="k", created_at=naive_utc_now()))
            await db.flush()
            await exposure.validate_exposure_credentials(db)
    with _env("tailnet"):
        async with db_session() as db:
            await exposure.validate_exposure_credentials(db)


# --- AC-G12.2: one loopback predicate ----------------------------------------------

#: IP literals only. A hostname rule such as library/plugin_policy.py's SSRF refusal of
#: the NAME ``localhost`` is a DNS-name check host_is_loopback (IP-only) cannot answer,
#: so it is not a second loopback predicate (audited for #1410).
_LOOPBACK_LITERALS = {"127.0.0.1", "::1"}


def test_no_loopback_literal_comparisons_outside_identity():
    """``identity.host_is_loopback`` is the only loopback predicate: no ``==``/``in``
    comparison against a loopback literal anywhere else in ``server/`` (defaults and
    URL construction are not comparisons and stay)."""
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in root.rglob("*.py"):
        if "tests" in path.parts:
            continue
        offenders += _loopback_comparisons(path.read_text(), str(path.relative_to(root)))
    assert offenders == [], offenders


def _loopback_comparisons(source: str, name: str) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        for side in [node.left, *node.comparators]:
            consts = [side] if isinstance(side, ast.Constant) else (
                list(side.elts) if isinstance(side, (ast.Tuple, ast.Set, ast.List)) else [])
            if any(isinstance(c, ast.Constant) and c.value in _LOOPBACK_LITERALS
                   for c in consts):
                out.append(f"{name}:{node.lineno}")
    return out


def test_loopback_guard_can_fail():
    """The guard above is proven able to fail on the shape it replaced (app.py's old
    ``_s.host in ("127.0.0.1", "localhost", "::1")``)."""
    assert _loopback_comparisons('x = h in ("127.0.0.1", "localhost", "::1")', "m") == ["m:1"]
    assert _loopback_comparisons('ok = h == "127.0.0.1"', "m") == ["m:1"]
    assert _loopback_comparisons('url = "http://127.0.0.1:8787"', "m") == []
