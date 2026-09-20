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
