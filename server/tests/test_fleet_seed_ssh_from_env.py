"""CCSWITCH_DAEMON_SSH_HOST/_REPO used to be the deploy path; now they only seed the
matching Host row's ssh_host/repo_path once, so an existing install keeps working."""
import asyncio
from sqlalchemy import select as sa_select


def test_seed_fills_null_ssh_columns_only(tmp_path, monkeypatch):
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.app import seed_host_ssh_from_env

    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/home/tony/GitHub/ccswitch")

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443"))
            db.add(Host(name="other", endpoint="https://o:9443", ssh_host="keep", repo_path="/keep"))
        await seed_host_ssh_from_env()
        async with db_session() as db:
            rows = (await db.execute(sa_select(Host).order_by(Host.name))).scalars().all()
        return {r.name: (r.ssh_host, r.repo_path) for r in rows}

    out = asyncio.run(run())
    assert out["rocky"] == ("tony@rocky", "/home/tony/GitHub/ccswitch")
    assert out["other"] == ("keep", "/keep")


def test_seed_never_overwrites_a_partially_configured_host(tmp_path, monkeypatch):
    """An operator who already set repo_path (by hand, or a prior partial seed) owns it —
    the seed must not clobber it even though ssh_host is still NULL and the name matches."""
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.app import seed_host_ssh_from_env

    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@rocky")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/home/tony/GitHub/ccswitch")

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443", ssh_host=None,
                        repo_path="/mine"))
        await seed_host_ssh_from_env()
        async with db_session() as db:
            row = (await db.execute(sa_select(Host).where(Host.name == "rocky"))).scalar_one()
        return (row.ssh_host, row.repo_path)

    assert asyncio.run(run()) == (None, "/mine")


def test_seed_is_noop_without_env_or_matching_host(tmp_path, monkeypatch):
    from server.db import db_session, init_db
    from server.db.models import Host
    from server.app import seed_host_ssh_from_env

    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_HOST", "tony@ghost")
    monkeypatch.setenv("CCSWITCH_DAEMON_SSH_REPO", "/srv/cc")

    async def run():
        await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / 'n.db'}")
        async with db_session() as db:
            db.add(Host(name="rocky", endpoint="https://rocky:9443"))
        await seed_host_ssh_from_env()
        async with db_session() as db:
            return (await db.execute(sa_select(Host.ssh_host).where(Host.name == "rocky"))).scalar_one()

    assert asyncio.run(run()) is None
