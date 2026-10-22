"""The dispatch-path trust gates key a user on the immutable GitHub id (#3147).

A policy lists ``login#<id>``; a mirrored row carries ``WorkItem.author_id``. A user who
registers a trusted user's freed login (the rename/squat case) shares the LOGIN but not
the id, so ``untrusted_refs`` and ``untrusted_prs`` must refuse them; a row with no
recorded id never earns user trust, while a Bot stays trusted by its ``app/<slug>``.
"""
from __future__ import annotations

import asyncio

from server.pm.dispatch_trust import untrusted_prs, untrusted_refs
from server.tests._trust_ids import TONY_ENTRY, TONY_ID

REPO = "o/r"


async def _seed():
    from server.db import db_session
    from server.db.models import Project, ProjectRepo, WorkItem
    async with db_session() as db:
        p = Project(slug="prod", name="prod",
                    automation_policy={"trusted_authors": [TONY_ENTRY, "app/viberpit"]})
        db.add(p)
        await db.flush()
        db.add(ProjectRepo(project_id=p.id, repo=REPO, is_primary=True))
        for number, kind, author, author_id in (
                (1, "issue", "tony", TONY_ID),
                (2, "issue", "tony", TONY_ID + 1),       # login matches, id differs
                (3, "issue", "tony", None),              # login matches, id unknown
                (4, "issue", "app/viberpit", None),      # a Bot: trusted by its app entry
                (1, "pr", "tony", TONY_ID),
                (2, "pr", "tony", TONY_ID + 1),
                (3, "pr", "tony", None),
                (4, "pr", "app/viberpit", None)):
            db.add(WorkItem(repo=REPO, number=number, kind=kind, state="open",
                            author=author, author_id=author_id))
        await db.commit()


def test_untrusted_refs_trusts_the_bound_id_not_the_login(isolated_db):
    async def go():
        from server.db import db_session
        await _seed()
        async with db_session() as db:
            return await untrusted_refs(db, [f"{REPO}#{n}" for n in (1, 2, 3, 4)])
    assert asyncio.run(go()) == [f"{REPO}#2", f"{REPO}#3"]


def test_untrusted_prs_trusts_the_bound_id_not_the_login(isolated_db):
    async def go():
        from server.db import db_session
        await _seed()
        async with db_session() as db:
            return await untrusted_prs(db, REPO, [1, 2, 3, 4])
    assert asyncio.run(go()) == [2, 3]
