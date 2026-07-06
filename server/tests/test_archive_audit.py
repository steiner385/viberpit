import asyncio
from server.automations import archive_audit as aa


def _row(id, goal, repo="o/r", branch="feat/x", authored=("feat/x",)):
    return {"id": id, "repo": repo, "branch": branch,
            "authored_branches": list(authored), "goal": goal, "goal_source": "explicit"}


def _gh_returning(pr):
    def gh_fn(repo, branch):
        return [pr] if pr else []
    return gh_fn


def _pr(number=1, state="OPEN", is_draft=False, auto_merge=False):
    return {"number": number, "state": state, "isDraft": is_draft,
            "autoMergeRequest": {"enabledAt": "t"} if auto_merge else None,
            "url": f"http://x/{number}"}


def test_merged_pr_is_legit_not_unarchived():
    unarchived = []
    gh = _gh_returning(_pr(state="MERGED"))
    out = asyncio.run(aa.run_audit([_row("s1", "fix ci")], gh, unarchived.append))
    assert out[0]["bucket"] == "legit" and unarchived == []


def test_unarmed_open_pr_is_premature_and_unarchived():
    unarchived = []
    gh = _gh_returning(_pr(state="OPEN", auto_merge=False))
    out = asyncio.run(aa.run_audit([_row("s1", "fix ci")], gh, unarchived.append))
    assert out[0]["bucket"] == "premature" and unarchived == ["s1"]


def test_code_goal_no_pr_is_ambiguous_report_only():
    unarchived = []
    out = asyncio.run(aa.run_audit([_row("s1", "fix the cicd doctor")],
                                   _gh_returning(None), unarchived.append))
    assert out[0]["bucket"] == "ambiguous" and unarchived == []


def test_gh_error_is_ambiguous():
    def gh(repo, branch):
        raise RuntimeError("gh down")
    out = asyncio.run(aa.run_audit([_row("s1", "fix ci")], gh, lambda s: None))
    assert out[0]["bucket"] == "ambiguous"


def test_dry_run_does_not_unarchive():
    unarchived = []
    gh = _gh_returning(_pr(state="OPEN", auto_merge=False))
    out = asyncio.run(aa.run_audit([_row("s1", "fix ci")], gh, unarchived.append, dry_run=True))
    assert out[0]["bucket"] == "premature" and unarchived == []


def test_real_gh_parses_json(monkeypatch):
    import subprocess
    sample = '[{"number": 3, "state": "OPEN", "isDraft": false, "autoMergeRequest": null, "url": "u"}]'

    def fake_run(cmd, capture_output, text, timeout, check):
        class R:
            stdout = sample
            returncode = 0
        assert "--head" in cmd and "--state" in cmd
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = aa._real_gh("o/r", "feat/x")
    assert out[0]["number"] == 3


def test_real_gh_returns_empty_on_error(monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(subprocess, "run", boom)
    assert aa._real_gh("o/r", "feat/x") == []


def test_load_archived_rows_maps_fields():
    class FakeRow:
        id = "s1"; repo = "o/r"; branch = "feat/x"
        authored_branches = ["feat/x", "feat/y"]
        active_goal = "fix ci"; inferred_goal = None; name = "sess"
        initial_prompt = None

    class FakeScalars:
        def all(self):
            return [FakeRow()]

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeDb:
        async def execute(self, _q):
            return FakeResult()

    class FakeFactory:
        def __call__(self):
            return self
        async def __aenter__(self):
            return FakeDb()
        async def __aexit__(self, *a):
            return False

    rows = asyncio.run(aa.load_archived_rows(FakeFactory()))
    assert rows[0]["id"] == "s1" and rows[0]["goal"] == "fix ci"
    assert rows[0]["authored_branches"] == ["feat/x", "feat/y"]


# ---------------------------------------------------------------------------
# main() integration tests
# ---------------------------------------------------------------------------

def test_main_dry_run_returns_0_with_archived_row(tmp_path, monkeypatch):
    """main(--dry-run) against a real temp-file DB with one archived row returns 0."""
    import uuid
    from server.db import init_db, db_session, dispose_db
    from server.db.models import Session as DbSession

    db_file = tmp_path / "audit_test.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    monkeypatch.setenv("CCSWITCH_DB_URL", db_url)
    monkeypatch.setattr(aa, "_real_gh", lambda repo, branch: [])

    async def run():
        await init_db(db_url=db_url)
        # Insert one archived session row
        async with db_session() as db:
            row = DbSession(
                id=str(uuid.uuid4()),
                name="test-session",
                account_name="test-account",
                status="archived",
                cwd="/tmp",
                repo="o/r",
                branch="feat/fix-ci",
                authored_branches=["feat/fix-ci"],
                active_goal="fix ci",
            )
            db.add(row)
        rc = await aa.main(["--dry-run"])
        await dispose_db()
        return rc

    assert asyncio.run(run()) == 0


def test_main_returns_2_when_db_url_unset(monkeypatch):
    """main() returns 2 (not a crash) when CCSWITCH_DB_URL is not set."""
    monkeypatch.delenv("CCSWITCH_DB_URL", raising=False)
    rc = asyncio.run(aa.main(["--dry-run"]))
    assert rc == 2
