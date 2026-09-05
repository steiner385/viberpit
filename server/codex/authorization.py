"""What the edge tells the credential authority, and in what order.

The authority cannot enumerate accounts. It has no read access to the Claude
account store, and by design it never will — a service that can read
``~/.ccswitch`` is a service that can be pointed at Anthropic credentials too.
So the edge projects a deliberately tiny view of the account manifest to it, and
that projection is the *only* thing that makes the authority willing to issue a
credential.

Two orderings live here, and getting them backwards is the whole risk:

* **Enabling** is manifest-first, then sync. If the sync fails, the account is
  enabled in ccswitch but not yet issuable — visible as degraded, retryable, and
  safe.
* **Disabling and removing** are revoke-first, then fan out manager
  invalidation, then write the manifest. If any step fails, the account's
  credentials are already dead. The unsafe order is the mirror image: writing
  "disabled" first and failing before the revoke leaves runners holding live
  envelopes for an account the operator believes is off.

:func:`build_authorization_sync` reads the manifest for identity and the *ledger*
for the enabled flag, because the ledger is the operative switch (see
``ccswitch.accounts.set_account_enabled``). Reading the manifest's mirror alone
would mean an authority that disagreed with the selector about which accounts are
live.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Final

from ccswitch import account_registry, ledger
from server.codex.auth_types import AuthorizationRecord, AuthorizationSync
from server.obs.context import log_event

logger = logging.getLogger(__name__)

#: How long an authorization projection stays valid without renewal. Short enough
#: that a dead edge stops being able to keep a disabled account alive within one
#: coffee break; long enough that a brief edge restart does not interrupt sessions.
DEFAULT_SYNC_LEASE_S: Final = 900.0
#: Renew well inside the lease so a single failed sync is not an outage.
DEFAULT_SYNC_INTERVAL_S: Final = 300.0


def build_authorization_sync(*, lease_s: float = DEFAULT_SYNC_LEASE_S) -> AuthorizationSync:
    """Project the account manifest into the authority's authorization view.

    Only OpenAI accounts are included. The authority has no business knowing that
    Anthropic or Copilot accounts exist, and sending them would make the
    projection a fleet-wide account inventory sitting in a service whose whole
    justification is a narrow blast radius.
    """
    manifest = account_registry.load_manifest()
    enabled = _enabled_flags()
    records = tuple(
        AuthorizationRecord(
            name=entry.name,
            provider=entry.provider,
            engine=entry.engine,
            # The ledger is operative; the manifest's own flag is its mirror. When
            # they disagree (a manifest write that lost to a runner, a restored
            # backup) take the STRICTER reading: a disabled account must not be
            # resurrected by a stale mirror.
            enabled=entry.enabled and enabled.get(entry.name, True),
        )
        for entry in sorted(manifest.accounts.values(), key=lambda e: e.name)
        if entry.provider == account_registry.PROVIDER_OPENAI
    )
    return AuthorizationSync(
        revision=manifest.revision, records=records, lease_s=lease_s
    )


def _enabled_flags() -> dict[str, bool]:
    """``{account: enabled}`` from the ledger, or an empty map on a read fault.

    An empty map means "the manifest mirror decides", which is the same degrade
    the rest of ``src/ccswitch`` takes on a store-mount fault: the account store
    lives on an sshfs mount whose transient EIO is an observed failure (#1005),
    and an authorization sync must degrade rather than mass-disable the fleet.
    """
    try:
        # read_ledger, NOT locked_ledger: the latter republishes the file on exit
        # even for a pure read, and this runs on a 300 s timer against a network
        # mount whose lock does not exclude across hosts. A periodic rewrite there
        # is a lost-update generator.
        led = ledger.read_ledger()
        return {
            name: ledger.is_account_enabled(led, name)
            for name in led.get("accounts", {})
        }
    except Exception:  # noqa: BLE001 -- a ledger fault must not break the sync
        logger.warning("codex authorization sync: ledger unreadable", exc_info=True)
        return {}


async def push_authorization(client: Any, *, lease_s: float = DEFAULT_SYNC_LEASE_S) -> Any:
    """Send the current projection. Returns the authority's health."""
    sync = build_authorization_sync(lease_s=lease_s)
    health = await client.authorize(sync)
    log_event(
        logger,
        logging.INFO,
        "codex.authorization.pushed",
        revision=sync.revision,
        accounts=len(sync.records),
    )
    return health


async def fan_out_invalidation(managers: Iterable[Any], account: str) -> list[str]:
    """Tell every runner manager to drop *account*'s credentials and stop it.

    Returns the list of failures as scrubbed strings — never raises. The caller
    reports a non-empty list as *degraded and retryable*: some runner may still
    hold a cached envelope, and the operator needs to know that rather than see a
    green "disabled". Best-effort per manager, because one unreachable host must
    not stop the other four from revoking.
    """
    from server.codex import redaction

    failures: list[str] = []
    for manager in managers:
        if manager is None:
            continue
        try:
            await manager.invalidate_credentials(account)
        except Exception as exc:  # noqa: BLE001 -- one host must not block the rest
            failures.append(redaction.scrub_exception(exc))
            log_event(
                logger,
                logging.ERROR,
                "codex.authorization.invalidate_failed",
                account=account,
                error=redaction.scrub_exception(exc),
            )
    return failures
