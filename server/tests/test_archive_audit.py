import asyncio
from server.automations import archive_audit as aa


def _row(id, goal, repo="o/r", branch="feat/x", authored=("feat/x",), initial_prompt=None):
    return {"id": id, "repo": repo, "branch": branch,
            "authored_branches": list(authored), "goal": goal, "goal_source": "explicit",
            "initial_prompt": initial_prompt}


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


# ---------------------------------------------------------------------------
# New tests for referenced-PR resolution (issue #502)
# ---------------------------------------------------------------------------

def test_referenced_pr_numbers_from_goal():
    """#N in goal body is extracted."""
    assert aa.referenced_pr_numbers("Fix CI in PR #11267", None) == [11267]


def test_referenced_pr_numbers_from_initial_prompt_only_own_pr():
    """'your pull request #N' in initial_prompt extracted; input-list PRs are NOT."""
    prompt = (
        "Your pull request cairnea/KinDash#11288 has failing CI. "
        "The PRs to combine (#11025, #11133)."
    )
    assert aa.referenced_pr_numbers("", prompt) == [11288]


def test_referenced_pr_numbers_empty_inputs():
    """Empty/None inputs return empty list."""
    assert aa.referenced_pr_numbers("", None) == []
    assert aa.referenced_pr_numbers("", "") == []


def _gh_num_returning(pr_dict):
    """Returns a gh_num_fn that ignores repo/number and returns the given dict (or None)."""
    def gh_num_fn(repo, number):
        return pr_dict
    return gh_num_fn


def test_referenced_merged_pr_is_legit():
    """A referenced MERGED PR → legit; session not unarchived."""
    unarchived = []
    row = _row("s1", "land PR #42 on the main branch")
    merged_pr = {"number": 42, "state": "MERGED", "isDraft": False,
                 "autoMergeRequest": None, "url": "http://x/42"}
    out = asyncio.run(aa.run_audit(
        [row],
        _gh_returning(None),   # branch lookup finds nothing
        unarchived.append,
        gh_num_fn=_gh_num_returning(merged_pr),
    ))
    assert out[0]["bucket"] == "legit"
    assert unarchived == []


def test_referenced_open_unarmed_pr_is_premature_and_unarchived():
    """A referenced open-unarmed PR → premature; session unarchived."""
    unarchived = []
    row = _row("s1", "land PR #42 on main")
    open_pr = {"number": 42, "state": "OPEN", "isDraft": False,
               "autoMergeRequest": None, "url": "http://x/42"}
    out = asyncio.run(aa.run_audit(
        [row],
        _gh_returning(None),
        unarchived.append,
        gh_num_fn=_gh_num_returning(open_pr),
    ))
    assert out[0]["bucket"] == "premature"
    assert unarchived == ["s1"]


def test_referenced_closed_pr_code_goal_is_review_not_unarchived():
    """A referenced CLOSED PR + code goal → 'review' bucket; action 'none'."""
    unarchived = []
    row = _row("s1", "implement feature in PR #42")
    closed_pr = {"number": 42, "state": "CLOSED", "isDraft": False,
                 "autoMergeRequest": None, "url": "http://x/42"}
    out = asyncio.run(aa.run_audit(
        [row],
        _gh_returning(None),
        unarchived.append,
        gh_num_fn=_gh_num_returning(closed_pr),
    ))
    assert out[0]["bucket"] == "review"
    assert out[0]["action"] == "none"
    assert unarchived == []


def test_no_pr_anywhere_code_goal_is_ambiguous():
    """No PR anywhere + code goal remains ambiguous (regression guard)."""
    unarchived = []
    row = _row("s1", "implement feature X")
    out = asyncio.run(aa.run_audit(
        [row],
        _gh_returning(None),
        unarchived.append,
        gh_num_fn=_gh_num_returning(None),
    ))
    assert out[0]["bucket"] == "ambiguous"
    assert unarchived == []


def test_build_pr_facts_dedupes_branch_and_ref_number():
    """A PR number matched by both branch lookup AND ref_numbers appears only once."""
    pr_raw = _pr(number=99, state="MERGED")
    facts = aa.build_pr_facts(
        "o/r",
        ["feat/x"],
        _gh_returning(pr_raw),
        ref_numbers=[99],
        gh_num_fn=_gh_num_returning(pr_raw),
    )
    assert len(facts.prs) == 1
    assert facts.prs[0].number == 99


def test_real_gh_num_parses_json(monkeypatch):
    """_real_gh_num returns a parsed dict on success."""
    import subprocess
    sample = '{"number": 42, "state": "OPEN", "isDraft": false, "autoMergeRequest": null, "url": "u"}'

    def fake_run(cmd, capture_output, text, timeout, check):
        class R:
            stdout = sample
            returncode = 0
        assert "pr" in cmd and "view" in cmd
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = aa._real_gh_num("o/r", 42)
    assert result is not None
    assert result["number"] == 42


def test_real_gh_num_returns_none_on_error(monkeypatch):
    """_real_gh_num returns None on any gh failure."""
    import subprocess

    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "gh")

    monkeypatch.setattr(subprocess, "run", boom)
    assert aa._real_gh_num("o/r", 42) is None
