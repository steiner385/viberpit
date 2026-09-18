"""The error-triage fingerprint FAMILY (spec 2026-09-18 §5).

``family_key`` is pure; ``families_for_refs`` reads the real ``error_signatures``
table (SQLite) and matches an issue ref against the repo error triage actually
files into — both halves of the ref, number AND repo, so a same-numbered issue in
another repository can never borrow a family.
"""
from __future__ import annotations

import asyncio

from server.db import db_session, init_db
from server.db.models import Automation as AutomationRow
from server.db.models import ErrorSignature
from server.pm.error_family import families_for_refs, family_key, triage_target_repo

TRIAGE_REPO = "steiner385/viberpit"  # the error-triage schema default


# --------------------------------------------------------------------------- #
# family_key (pure)                                                            #
# --------------------------------------------------------------------------- #

def test_family_key_joins_logger_and_event():
    assert family_key("server.pm.queue", "queue.load_failed") == (
        "server.pm.queue:queue.load_failed")


def test_family_key_needs_both_halves():
    assert family_key(None, "queue.load_failed") is None
    assert family_key("server.pm.queue", None) is None
    assert family_key(None, None) is None


def test_family_key_ignores_blank_halves():
    """A whitespace-only column is as absent as NULL — otherwise every blank-event
    signature in the table would land in one enormous ``logger: `` family."""
    assert family_key("server.pm.queue", "   ") is None
    assert family_key("", "queue.load_failed") is None


# --------------------------------------------------------------------------- #
# families_for_refs (real SQLite)                                              #
# --------------------------------------------------------------------------- #

async def _seed(tmp_path, name: str, signatures: list[dict],
                target_repo: str | None = None) -> None:
    await init_db(db_url=f"sqlite+aiosqlite:///{tmp_path / name}")
    async with db_session() as db:
        for i, sig in enumerate(signatures):
            db.add(ErrorSignature(fingerprint=f"fp{i}", source="edge",
                                  level="ERROR", **sig))
        if target_repo is not None:
            db.add(AutomationRow(id="error-triage", enabled=True, mode="file",
                                 config={"target_repo": target_repo}))
        await db.flush()


async def _families(refs: list[str]) -> dict[str, str]:
    """The caller's two steps: resolve the triage repo through the config store,
    then join inside one sync session — the shape the orchestrator uses."""
    repo = await triage_target_repo()
    async with db_session() as db:
        return await db.run_sync(lambda s: families_for_refs(s, refs, repo=repo))


def test_families_group_siblings_and_skip_unfiled_signatures(tmp_path):
    async def run():
        await _seed(tmp_path, "fam.db", [
            # two DIFFERENT fingerprints of one family, each with its own issue
            {"issue_number": 101, "logger": "server.a", "event": "boom"},
            {"issue_number": 102, "logger": "server.a", "event": "boom"},
            # a different family
            {"issue_number": 103, "logger": "server.b", "event": "crash"},
            # filed, but no family key derivable
            {"issue_number": 104, "logger": "server.c", "event": None},
            # never filed — its number column is NULL, so no ref can match it
            {"issue_number": None, "logger": "server.d", "event": "quiet"},
        ])
        refs = [f"{TRIAGE_REPO}#{n}" for n in (101, 102, 103, 104, 105)]

        out = await _families(refs)

        assert out == {
            f"{TRIAGE_REPO}#101": "server.a:boom",
            f"{TRIAGE_REPO}#102": "server.a:boom",
            f"{TRIAGE_REPO}#103": "server.b:crash",
        }

    asyncio.run(run())


def test_families_match_on_repo_not_just_number(tmp_path):
    """Issue numbers are only unique within a repository: another repo's #101 must
    not inherit the triage repo's family."""
    async def run():
        await _seed(tmp_path, "repo.db", [
            {"issue_number": 101, "logger": "server.a", "event": "boom"},
        ])

        out = await _families([f"{TRIAGE_REPO}#101", "other/repo#101", "nonsense"])

        assert out == {f"{TRIAGE_REPO}#101": "server.a:boom"}

    asyncio.run(run())


def test_families_follow_the_configured_triage_repo(tmp_path):
    """The repo comes from the error-triage automation's own config — the one
    place that decides where auto-error issues are filed."""
    async def run():
        await _seed(tmp_path, "cfg.db", [
            {"issue_number": 7, "logger": "server.a", "event": "boom"},
        ], target_repo="acme/widgets")

        out = await _families(["acme/widgets#7", f"{TRIAGE_REPO}#7"])

        assert out == {"acme/widgets#7": "server.a:boom"}

    asyncio.run(run())


def test_families_of_no_refs_reads_nothing(tmp_path):
    async def run():
        await _seed(tmp_path, "empty.db", [
            {"issue_number": 101, "logger": "server.a", "event": "boom"},
        ])

        assert await _families([]) == {}

    asyncio.run(run())


def test_families_without_a_triage_repo_match_nothing(tmp_path):
    """No configured target repo means no ref can be an auto-error report — the
    fail-open answer, not a match on issue number alone."""
    async def run():
        await _seed(tmp_path, "norepo.db", [
            {"issue_number": 101, "logger": "server.a", "event": "boom"},
        ])
        async with db_session() as db:
            out = await db.run_sync(
                lambda s: families_for_refs(s, [f"{TRIAGE_REPO}#101"], repo=""))
        assert out == {}

    asyncio.run(run())


def test_triage_target_repo_defaults_to_the_schema_value(tmp_path):
    async def run():
        await _seed(tmp_path, "defrepo.db", [])
        assert await triage_target_repo() == TRIAGE_REPO

    asyncio.run(run())
