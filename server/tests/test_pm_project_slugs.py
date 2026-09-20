"""Slug-alias resolution for renamed products (`server/pm/project_slugs.py`, #1881)."""
from __future__ import annotations

import asyncio

from server.pm.project_slugs import former_slugs_after_rename

# The resolution tests below want a schema, not an app: conftest's `isolated_db`
# is the canonical per-test SQLite redirect (#728). No TestClient, so `asyncio.run`
# is the only loop in play.


# -- the alias list -----------------------------------------------------------

def test_rename_appends_the_old_slug_oldest_first():
    assert former_slugs_after_rename(
        None, old_slug="example-old", new_slug="example-new") == ["example-old"]
    assert former_slugs_after_rename(
        ["a"], old_slug="b", new_slug="c") == ["a", "b"]


def test_readopting_a_slug_does_not_make_it_its_own_alias():
    """A→B→A: after the second rename the product is `a` again, so `a` must not
    still be listed as former — a live slug that is also its own alias would let
    `resolve_project_slugs` reach it by two paths and makes the history a lie."""
    after_first = former_slugs_after_rename(None, old_slug="a", new_slug="b")
    assert after_first == ["a"]
    after_second = former_slugs_after_rename(after_first, old_slug="b", new_slug="a")
    assert after_second == ["b"]


def test_repeated_renames_do_not_accumulate_duplicates():
    hist: list[str] = []
    for old, new in (("a", "b"), ("b", "a"), ("a", "b"), ("b", "c")):
        hist = former_slugs_after_rename(hist, old_slug=old, new_slug=new)
    assert hist == ["a", "b"]


def test_a_noop_rename_records_nothing():
    assert former_slugs_after_rename(None, old_slug="a", new_slug="a") == []
    assert former_slugs_after_rename(None, old_slug=None, new_slug="a") == []


def test_junk_in_the_stored_list_is_dropped_not_raised():
    """The column is JSON, so a hand-edited or legacy row can hold anything.
    Resolution must degrade, never crash a pm-sync tick."""
    assert former_slugs_after_rename(
        ["ok", "", None, 7, {"x": 1}], old_slug="b", new_slug="c") == ["ok", "b"]


# -- resolution ---------------------------------------------------------------

def _seed(rows: list[tuple[str | None, list[str] | None]]) -> dict[str, str]:
    from server.db import db_session
    from server.db.models import Project

    async def go() -> dict[str, str]:
        async with db_session() as db:
            made = [Project(slug=slug, name=slug or "x", former_slugs=former)
                    for slug, former in rows]
            db.add_all(made)
            await db.flush()
            ids = {str(p.slug): str(p.id) for p in made}
            await db.commit()
            return ids
    return asyncio.run(go())


def _resolve(slugs: list[str]) -> dict[str, str]:
    from server.db import db_session
    from server.pm.project_slugs import resolve_project_slugs

    async def go() -> dict[str, str]:
        async with db_session() as db:
            return await resolve_project_slugs(db, slugs)
    return asyncio.run(go())


def test_a_former_slug_resolves_to_the_renamed_product(isolated_db):
    ids = _seed([("example-new", ["example-old"])])
    assert _resolve(["example-old"]) == {"example-old": ids["example-new"]}
    assert _resolve(["example-new"]) == {"example-new": ids["example-new"]}


def test_a_live_slug_always_beats_another_product_s_alias(isolated_db):
    """The freed-up slug can be adopted by a new product. New docs naming it must
    reach the new product; only docs the rename orphaned fall through to the
    alias, so there is never an ambiguous answer."""
    ids = _seed([("example-new", ["example-old"]), ("example-old", None)])
    assert _resolve(["example-old"]) == {"example-old": ids["example-old"]}


def test_an_alias_shared_by_two_products_resolves_to_neither(isolated_db):
    """No principled winner, and guessing would flip the edge between ticks —
    `_pm_sync_apply_feature_plan` rebuilds links every apply."""
    _seed([("one", ["shared"]), ("two", ["shared"])])
    assert _resolve(["shared"]) == {}


def test_unknown_slugs_are_absent_rather_than_an_error(isolated_db):
    ids = _seed([("example-new", ["example-old"])])
    assert _resolve(["example-new", "nope"]) == {"example-new": ids["example-new"]}
    assert _resolve([]) == {}
    assert _resolve([""]) == {}


# -- reading the rename history back out of the audit log ---------------------

def test_only_slug_changes_are_read_as_renames():
    """`pm.project.update` also records name changes, repo repoints and priority
    nudges; none of those is a rename, and a partially-shaped detail from an
    older endpoint must be skipped rather than crash startup."""
    from server.pm.project_slugs import renames_from_audit

    assert renames_from_audit([
        {"slug": "b", "changes": {"slug": {"from": "a", "to": "b"}}},
        {"slug": "b", "changes": {"name": {"from": "A", "to": "B"}}},
        {"slug": "b", "changes": {"repo": {"from": "o/a", "to": "o/b"}}},
        {"slug": "b", "changes": {"priorityRank": {"from": 3, "to": 4}}},
        {"slug": "b"},                                   # no changes block
        {"slug": "b", "changes": "not-a-mapping"},
        {"slug": "b", "changes": {"slug": "not-a-mapping"}},
        {"slug": "b", "changes": {"slug": {"from": None, "to": "c"}}},
        {"slug": "b", "changes": {"slug": {"to": "c"}}},
        "not-a-mapping",
        None,
    ]) == [("a", "b")]


def test_a_rename_chain_collapses_to_the_slug_it_ends_on():
    """The audit names slugs, never ids, so a product is followed by the slug
    each rename lands on — and the list reads oldest first."""
    from server.pm.project_slugs import rename_history

    assert rename_history([("a", "b"), ("b", "c")]) == {"c": ["a", "b"]}
    assert rename_history([]) == {}
    # A→B→A: the product is `a` again, so only `b` is former.
    assert rename_history([("a", "b"), ("b", "a")]) == {"a": ["b"]}


def test_adopting_a_freed_slug_starts_a_new_chain():
    """`foo→bar` then `baz→foo` is TWO products. Extending the first chain would
    hand the adopter's history to the product that left the slug behind, and the
    two would then claim each other's aliases."""
    from server.pm.project_slugs import rename_history

    assert rename_history([("foo", "bar"), ("baz", "foo")]) == {
        "bar": ["foo"], "foo": ["baz"]}


def _audit_renames(pairs: list[tuple[str, str]]) -> None:
    """Record *pairs* as `pm.project.update` audit rows, oldest first."""
    from datetime import datetime, timedelta, timezone

    from server.db import db_session
    from server.db.models import AuditEvent
    from server.pm.project_slugs import PROJECT_UPDATE_AUDIT_TYPE

    async def go() -> None:
        # Relative, not a date literal: only the ORDER of these rows matters,
        # and an absolute one would be a time bomb the day something starts
        # comparing audit rows against the wall clock.
        # `at` is naive UTC like every other timestamp in this schema.
        base = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        async with db_session() as db:
            for i, (old, new) in enumerate(pairs):
                db.add(AuditEvent(
                    at=base + timedelta(minutes=i), type=PROJECT_UPDATE_AUDIT_TYPE,
                    identity="tester",
                    detail={"slug": new, "changes": {"slug": {"from": old, "to": new}}}))
    asyncio.run(go())


def _backfill() -> dict[str, list[str]]:
    from server.db import db_session
    from server.pm.project_slugs import backfill_former_slugs

    async def go() -> dict[str, list[str]]:
        async with db_session() as db:
            return await backfill_former_slugs(db)
    return asyncio.run(go())


def test_a_rename_made_before_the_column_existed_still_resolves(isolated_db):
    """#1880: the alias column only ever filled forward, so the renames that
    motivated it — including one made hours before it shipped — stayed
    unprotected and the next doc naming the old slug would lose its edge.

    The assertion is on RESOLUTION, not on the column: the stored list is
    bookkeeping, reaching the product by its old slug is the property.
    """
    ids = _seed([("example-new", None)])
    _audit_renames([("example-old", "example-new")])
    assert _resolve(["example-old"]) == {}, "precondition: the alias is missing"

    assert _backfill() == {"example-new": ["example-old"]}
    assert _resolve(["example-old"]) == {"example-old": ids["example-new"]}


def test_the_backfill_is_idempotent_and_leaves_recorded_aliases_alone(isolated_db):
    """It runs on every startup. A second pass must write nothing, and a rename
    the endpoint DID record must keep its place.

    Two renames, straddling the column: `example-old→example-mid` left only an
    audit row, then `example-mid→example-new` also wrote `former_slugs`. Both
    are in the audit — the endpoint records there too — so the recovered chain
    overlaps what is stored, and the older, audit-only slug has to sort AHEAD of
    it rather than being appended after the slug that came later.
    """
    _seed([("example-new", ["example-mid"])])
    _audit_renames([("example-old", "example-mid"), ("example-mid", "example-new")])

    assert _backfill() == {"example-new": ["example-old", "example-mid"]}
    assert _backfill() == {}, "a second startup must not rewrite the column"


def test_the_backfill_ignores_renames_of_a_product_that_no_longer_exists(isolated_db):
    """A deleted product's rename history has nowhere to land; it must not be
    grafted onto whichever product happens to hold a slug from its chain."""
    _seed([("survivor", None)])
    _audit_renames([("gone-old", "gone-new")])
    assert _backfill() == {}
    assert _resolve(["gone-old"]) == {}
