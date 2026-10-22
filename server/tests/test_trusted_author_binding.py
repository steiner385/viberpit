"""#3147: ``trusted_authors`` binds each user login to its immutable GitHub user id.

A GitHub login can be renamed and the freed name re-registered by anyone, so a policy
entry is stored as ``login#<id>`` (bound on write through ``GET /users/{login}``) and the
queue-shaped reading trusts users only by id (``user:<id>``)."""
from __future__ import annotations

import logging
import subprocess

import pytest
from sqlalchemy import select

from server.automations.product_policy import (
    bind_unbound_trusted_authors, merge_policy_patch, patch_project_policy, trusted_authors,
    trusted_user_logins)
from server.db.models import Project
from server.pm import github_identity
from server.pm.github_identity import GitHubLookupError

# Captured at import, before the conftest guard swaps the module attribute for one
# that refuses: these tests drive the real resolver through a fake ``gh`` runner.
_real_resolve = github_identity.resolve_github_user_id


def _fake_resolver(ids: dict[str, int]):
    calls: list[str] = []

    async def resolve(login: str) -> int | None:
        calls.append(login)
        return ids.get(login.lower())
    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def _proc(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], rc, stdout=stdout, stderr=stderr)


# --- the resolver ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolver_returns_the_id_of_a_user():
    seen: list[list[str]] = []

    def run(args, token=None, **kw):
        seen.append(args)
        return _proc(0, '{"login": "Tony", "id": 1234567, "type": "User"}')
    assert await _real_resolve("Tony", run=run) == 1234567
    assert seen == [["api", "users/Tony"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["Organization", "Bot"])
async def test_resolver_refuses_a_non_user_account(kind):
    def run(args, token=None, **kw):
        return _proc(0, f'{{"login": "acme", "id": 9, "type": "{kind}"}}')
    assert await _real_resolve("acme", run=run) is None


@pytest.mark.asyncio
async def test_resolver_reads_a_404_as_no_such_user():
    def run(args, token=None, **kw):
        return _proc(1, stderr="gh: Not Found (HTTP 404)")
    assert await _real_resolve("nobody-here", run=run) is None


@pytest.mark.asyncio
async def test_resolver_raises_on_a_transport_or_auth_failure():
    def run(args, token=None, **kw):
        return _proc(1, stderr="HTTP 401: Bad credentials")
    with pytest.raises(GitHubLookupError):
        await _real_resolve("tony", run=run)


@pytest.mark.asyncio
@pytest.mark.parametrize("login", ["../repos/x", "a/b", "tony?x=1", "", "-tony", "a" * 40])
async def test_resolver_never_sends_a_non_login_to_the_api(login):
    def run(args, token=None, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("a non-login reached gh")
    with pytest.raises(ValueError):
        await _real_resolve(login, run=run)


@pytest.mark.asyncio
async def test_conftest_guard_refuses_real_lookups():
    with pytest.raises(GitHubLookupError):
        await github_identity.resolve_github_user_id("tony")


# --- the readers -------------------------------------------------------------------

def test_trusted_authors_trusts_users_only_by_id():
    policy = {"trusted_authors": [" Steiner385#1234 ", "tony", "", "viberpit", "github-actions",
                                  "App/Viberpit", "x#abc", "dependabot[bot]", "Alice#77"]}
    assert trusted_authors(policy) == frozenset({"user:1234", "user:77", "app/viberpit"})
    assert trusted_authors(None) == frozenset()


def test_trusted_user_logins_are_display_only_logins_bound_or_not():
    policy = {"trusted_authors": ["Steiner385#1234", " Tony ", "app/renovate", "viberpit",
                                  "dependabot[bot]", "x#abc"]}
    assert trusted_user_logins(policy) == frozenset({"steiner385", "tony"})
    assert trusted_user_logins(None) == frozenset()


# --- write-time validation -----------------------------------------------------------

def test_merge_accepts_a_bound_entry_as_written():
    merged = merge_policy_patch(None, {"trusted_authors": ["Tony#1234567", "app/renovate"]})
    assert merged == {"trusted_authors": ["Tony#1234567", "app/renovate"]}


@pytest.mark.parametrize("entry", ["tony#0", "tony#-1", "tony#abc", "#5", "tony#", "a#1#2",
                                   "viberpit#5", "github-actions#7"])
def test_merge_rejects_a_malformed_or_squatter_bound_entry(entry):
    with pytest.raises(ValueError, match="trusted_authors"):
        merge_policy_patch(None, {"trusted_authors": [entry]})


# --- binding on patch ----------------------------------------------------------------

async def _project(db, policy=None) -> Project:
    p = Project(id="p", slug="p", name="p", automation_policy=policy)
    db.add(p)
    await db.flush()
    return p


@pytest.mark.asyncio
async def test_patch_binds_bare_logins_to_their_ids(isolated_db):
    from server.db import db_session
    resolve = _fake_resolver({"tony": 1234567, "alice": 42})
    async with db_session() as db:
        p = await _project(db)
        merged = await patch_project_policy(
            db, p, {"trusted_authors": [" Tony ", "app/renovate", "bob#9", "alice"]},
            resolve_user_id=resolve)
    assert merged == {"trusted_authors": ["tony#1234567", "app/renovate", "bob#9", "alice#42"]}
    assert resolve.calls == ["tony", "alice"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("login", ["ghost-user", "acme-org"])
async def test_patch_refuses_an_unresolvable_login(isolated_db, login):
    """A 404 and an Organization/Bot both resolve to None: a 422, never stored unbound."""
    from server.db import db_session
    async with db_session() as db:
        p = await _project(db)
        with pytest.raises(ValueError, match=f"GitHub user '{login}' not found"):
            await patch_project_policy(db, p, {"trusted_authors": [login]},
                                       resolve_user_id=_fake_resolver({}))


@pytest.mark.asyncio
async def test_patch_rejects_a_bare_app_login_without_a_lookup(isolated_db):
    from server.db import db_session
    resolve = _fake_resolver({"viberpit": 5})
    async with db_session() as db:
        p = await _project(db)
        with pytest.raises(ValueError, match="app/viberpit"):
            await patch_project_policy(db, p, {"trusted_authors": ["viberpit"]},
                                       resolve_user_id=resolve)
    assert resolve.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_patch_without_bare_users_never_calls_the_resolver(isolated_db):
    from server.db import db_session

    async def resolve(login):  # pragma: no cover - must not be reached
        raise AssertionError("no lookup expected")
    async with db_session() as db:
        p = await _project(db)
        assert await patch_project_policy(db, p, {"dispatch": "auto"},
                                          resolve_user_id=resolve) == {"dispatch": "auto"}
        assert await patch_project_policy(
            db, p, {"trusted_authors": ["tony#1", "app/x"]}, resolve_user_id=resolve) == {
            "dispatch": "auto", "trusted_authors": ["tony#1", "app/x"]}


@pytest.mark.asyncio
async def test_patch_propagates_a_lookup_failure(isolated_db):
    from server.db import db_session

    async def resolve(login):
        raise GitHubLookupError("down")
    async with db_session() as db:
        p = await _project(db)
        with pytest.raises(GitHubLookupError):
            await patch_project_policy(db, p, {"trusted_authors": ["tony"]},
                                       resolve_user_id=resolve)


# --- the startup binder ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_startup_binder_binds_once_and_is_then_a_no_op(isolated_db, caplog):
    from server.db import db_session
    async with db_session() as db:
        db.add(Project(id="a", slug="a", name="a", automation_policy={
            "dispatch": "auto",
            "trusted_authors": ["Tony", "app/renovate", "viberpit", "dependabot[bot]", "bob#9"]}))
        db.add(Project(id="b", slug="b", name="b", automation_policy={
            "trusted_authors": ["gone-user"]}))
        db.add(Project(id="c", slug="c", name="c", automation_policy={"dispatch": "auto"}))
        db.add(Project(id="d", slug="d", name="d", automation_policy=None))
    resolve = _fake_resolver({"tony": 1234567})
    with caplog.at_level(logging.WARNING):
        async with db_session() as db:
            bound = await bind_unbound_trusted_authors(db, resolve_user_id=resolve)
    assert bound == ["a"]
    assert [r for r in caplog.records if r.levelno == logging.WARNING and "'b'" in r.getMessage()]
    async with db_session() as db:
        rows = {p.slug: p.automation_policy for p in (await db.scalars(select(Project))).all()}
    # Inert entries (a bare app login, a REST bot login) contributed nothing on read; the
    # binder drops them rather than fail the whole product's re-save on them.
    assert rows["a"] == {"dispatch": "auto",
                         "trusted_authors": ["tony#1234567", "app/renovate", "bob#9"]}
    assert rows["b"] == {"trusted_authors": ["gone-user"]}  # unchanged, retried next start

    resolve2 = _fake_resolver({"tony": 1234567, "gone-user": 5})
    async with db_session() as db:
        assert await bind_unbound_trusted_authors(db, resolve_user_id=resolve2) == ["b"]
    assert resolve2.calls == ["gone-user"]  # type: ignore[attr-defined]
    async with db_session() as db:
        assert await bind_unbound_trusted_authors(db, resolve_user_id=resolve2) == []
    assert resolve2.calls == ["gone-user"]  # type: ignore[attr-defined]
