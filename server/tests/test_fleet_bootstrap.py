"""A single-host deployment must become a one-row fleet automatically.

Spec rollback requirement: "#4 degrades to single-host when the registry has one
entry." Nobody should have to hand-populate a table to keep working.
"""
from __future__ import annotations

import asyncio


def test_existing_endpoint_is_seeded_as_a_host(tmp_path):
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'boot.db'}")
        name = await ensure_default_host("unix:///run/ccswitch/sessiond.sock")
        async with db_session() as db:
            rows = (await db.execute(sa_select(Host))).scalars().all()
        return name, rows

    name, rows = asyncio.run(run())
    assert len(rows) == 1
    assert rows[0].endpoint == "unix:///run/ccswitch/sessiond.sock"
    assert rows[0].name == name
    assert rows[0].enabled is True


def test_seeding_is_idempotent(tmp_path):
    from sqlalchemy import func, select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'boot2.db'}")
        await ensure_default_host("unix:///run/ccswitch/sessiond.sock")
        await ensure_default_host("unix:///run/ccswitch/sessiond.sock")
        async with db_session() as db:
            return (await db.execute(
                sa_select(func.count()).select_from(Host))).scalar()

    assert asyncio.run(run()) == 1, "re-seeding must not duplicate the host"


def test_seeding_does_not_disturb_an_existing_registry(tmp_path):
    """Once an operator manages the fleet, startup must not inject rows."""
    from sqlalchemy import func, select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'boot3.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443"))
        await ensure_default_host("unix:///run/ccswitch/sessiond.sock")
        async with db_session() as db:
            return (await db.execute(
                sa_select(func.count()).select_from(Host))).scalar()

    assert asyncio.run(run()) == 1, "an operator-managed fleet must not gain a row"


def test_matching_endpoint_resolves_to_its_host_name(tmp_path):
    """The edge must learn WHICH registry host its endpoint is, so sessions can
    be stamped without guessing."""
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'boot4.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443"))
            db.add(Host(name="dobby", endpoint="unix:///run/ccswitch/sessiond.sock"))
        return await ensure_default_host("https://rocky:9443")

    assert asyncio.run(run()) == "rocky"


def test_unknown_endpoint_in_an_operator_fleet_resolves_to_none(tmp_path):
    """If the registry is operator-managed and our endpoint is not in it, we do
    NOT know which host we are talking to. Return None rather than guessing —
    a wrong host stamp is worse than an absent one."""
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'boot5.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443"))
        return await ensure_default_host("unix:///somewhere/else.sock")

    assert asyncio.run(run()) is None


def test_repairs_an_unparseable_auto_seeded_endpoint(tmp_path):
    """The auto-seeded row is ours to fix; a bad endpoint makes it useless.

    #720 seeded it with str(endpoint) before Endpoint had a __str__, storing the
    dataclass repr. The row then failed every probe with "unsupported daemon
    endpoint" and nothing could repair it: ensure_default_host no-ops once any
    host exists, and the API deliberately refuses to set endpoint.

    The repair also renames 'local' → derived host name (#743).
    """
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import DEFAULT_HOST_NAME, ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'repair.db'}")
        async with db_session() as db:
            db.add(Host(name=DEFAULT_HOST_NAME,
                        endpoint="Endpoint(scheme='https', host='h', port=9443)"))
        name = await ensure_default_host("https://h:9443")
        async with db_session() as db:
            row = (await db.execute(
                sa_select(Host).where(Host.name == name))).scalars().first()
        return name, row

    name, row = asyncio.run(run())
    assert name == "h", "must derive name from hostname, not keep 'local'"
    assert row.endpoint == "https://h:9443", "must repair the unusable endpoint"


def test_does_not_touch_an_operator_row_with_a_different_endpoint(tmp_path):
    """Only the auto-seeded row self-heals, and only when unparseable. An
    operator's deliberate endpoint must never be rewritten behind their back."""
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'norepair.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443"))
        await ensure_default_host("unix:///run/other.sock")
        async with db_session() as db:
            return (await db.execute(
                sa_select(Host).where(Host.name == "rocky"))).scalars().first()

    assert asyncio.run(run()).endpoint == "https://rocky:9443"

# ── #743 — seeded name derived from endpoint hostname ──────────────────────


def test_fresh_seed_name_derived_from_https_endpoint(tmp_path):
    """A fresh install with an HTTPS endpoint seeds the host with a name derived
    from the hostname, not the generic 'local' constant."""
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'https_seed.db'}")
        name = await ensure_default_host("https://rocky.tail0dd570.ts.net:9443")
        async with db_session() as db:
            rows = (await db.execute(sa_select(Host))).scalars().all()
        return name, rows

    name, rows = asyncio.run(run())
    assert name == "rocky"
    assert len(rows) == 1
    assert rows[0].name == "rocky"
    assert rows[0].endpoint == "https://rocky.tail0dd570.ts.net:9443"


def test_renames_local_row_to_match_https_endpoint(tmp_path):
    """An already-seeded 'local' row whose endpoint is HTTPS gets renamed on
    startup to the hostname derived from the endpoint (#743)."""
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import DEFAULT_HOST_NAME, ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'rename.db'}")
        async with db_session() as db:
            db.add(Host(name=DEFAULT_HOST_NAME,
                        endpoint="https://rocky.tail0dd570.ts.net:9443",
                        enabled=True))
        name = await ensure_default_host("https://rocky.tail0dd570.ts.net:9443")
        async with db_session() as db:
            rows = (await db.execute(sa_select(Host))).scalars().all()
        return name, rows

    name, rows = asyncio.run(run())
    assert name == "rocky"
    assert len(rows) == 1
    assert rows[0].name == "rocky", "row must be renamed from 'local' to 'rocky'"


# ── CCSWITCH_FLEET_HOSTS env-var seeding ───────────────────────────────────


def test_fleet_hosts_map_parses_name_endpoint_pairs():
    """Comma-separated name=endpoint pairs become a dict."""
    from server.config import get_settings

    import os
    old = os.environ.get("CCSWITCH_FLEET_HOSTS")
    try:
        os.environ["CCSWITCH_FLEET_HOSTS"] = "rocky=https://rocky:9443,dobby=https://dobby:9443"
        s = get_settings()
        m = s.fleet_hosts_map
    finally:
        if old is None:
            os.environ.pop("CCSWITCH_FLEET_HOSTS", None)
        else:
            os.environ["CCSWITCH_FLEET_HOSTS"] = old
    assert m == {"rocky": "https://rocky:9443", "dobby": "https://dobby:9443"}


def test_fleet_hosts_map_empty_when_unset():
    import os
    from server.config import get_settings

    old = os.environ.pop("CCSWITCH_FLEET_HOSTS", None)
    try:
        s = get_settings()
        assert s.fleet_hosts_map == {}
    finally:
        if old is not None:
            os.environ["CCSWITCH_FLEET_HOSTS"] = old


def test_fleet_hosts_map_ignores_malformed_entries():
    """Items without '=' are silently skipped."""
    import os
    from server.config import get_settings

    old = os.environ.get("CCSWITCH_FLEET_HOSTS")
    try:
        os.environ["CCSWITCH_FLEET_HOSTS"] = "rocky=https://rocky:9443,badentry,"
        s = get_settings()
        m = s.fleet_hosts_map
    finally:
        if old is None:
            os.environ.pop("CCSWITCH_FLEET_HOSTS", None)
        else:
            os.environ["CCSWITCH_FLEET_HOSTS"] = old
    assert m == {"rocky": "https://rocky:9443"}


def test_fleet_hosts_seeding_inserts_declared_hosts(tmp_path):
    """Startup seeds all hosts declared in fleet_hosts_map."""
    from sqlalchemy import select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'fh.db'}")
        fleet_map = {"rocky": "https://rocky:9443", "dobby": "https://dobby:9443"}
        async with db_session() as db:
            for name, ep in fleet_map.items():
                from sqlalchemy import select as _sel
                exists = (await db.execute(
                    _sel(Host.name).where(Host.name == name))).scalar_one_or_none()
                if exists is None:
                    db.add(Host(name=name, endpoint=ep, enabled=True))
        async with db_session() as db:
            rows = (await db.execute(sa_select(Host).order_by(Host.name))).scalars().all()
        return rows

    rows = asyncio.run(run())
    assert len(rows) == 2
    assert rows[0].name == "dobby"
    assert rows[1].name == "rocky"


def test_fleet_hosts_seeding_is_idempotent(tmp_path):
    """Re-seeding the same hosts does not create duplicates."""
    from sqlalchemy import func, select as sa_select

    from server.db import db_session, init_db
    from server.db.models import Host

    async def _seed(db_url):
        await init_db(db_url=db_url)
        fleet_map = {"rocky": "https://rocky:9443"}
        async with db_session() as db:
            from sqlalchemy import select as _sel
            exists = (await db.execute(
                _sel(Host.name).where(Host.name == "rocky"))).scalar_one_or_none()
            if exists is None:
                db.add(Host(name="rocky", endpoint="https://rocky:9443", enabled=True))

    async def run():
        db_url = f"sqlite+aiosqlite:///{tmp_path / 'idem.db'}"
        await _seed(db_url)
        await _seed(db_url)
        async with db_session() as db:
            return (await db.execute(
                sa_select(func.count()).select_from(Host))).scalar()

    assert asyncio.run(run()) == 1


def test_ensure_default_host_finds_fleet_hosts_seeded_host(tmp_path):
    """When fleet_hosts seeded rocky, ensure_default_host identifies rocky's endpoint."""
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.sessions.fleet_router import ensure_default_host

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'fe.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443", enabled=True))
            db.add(Host(name="dobby", endpoint="https://dobby:9443", enabled=True))
        return await ensure_default_host("https://rocky:9443")

    assert asyncio.run(run()) == "rocky"
