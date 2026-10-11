"""Session Feedback inbox, part 3 (#2936): "possibly fixed by #N" from the mirrored
merged PRs, the age-out digest + restore, and session links in drafted issues.

Same fixtures as the filer and inbox tests: a fake gh runner, a fixed clock, a
scratch SQLite DB; no network, no model."""
from __future__ import annotations

from datetime import timedelta, timezone

from server.automations.context import AutomationContext
from server.automations.session_feedback import SessionFeedbackActionProvider, SessionFeedbackFiler
from server.db import db_session
from server.db.models import WorkItem
from server.tests.test_session_feedback_filer import NOW, FakeGh, _loop, _row, _run, _seed
from server.tests.test_session_feedback_inbox import _seed_theme


def _ctx():
    return AutomationContext("session-feedback")


async def _pr(number, title, *, merged_at, repo="o/r", state="merged", body=""):
    """A mirrored PR as github_sync writes it: gh_closed_at tz-aware UTC."""
    async with db_session() as db:
        db.add(WorkItem(repo=repo, number=number, kind="pr", state=state, title=title,
                        body=body, url=f"https://github.com/{repo}/pull/{number}",
                        gh_closed_at=merged_at.replace(tzinfo=timezone.utc)))


def _by_members(view):
    return {tuple(sorted(m["id"] for m in c["members"])): c for c in view["clusters"]}


# -- possibly fixed -------------------------------------------------------------

def test_possibly_fixed_names_related_prs_merged_after_the_first_report(tmp_path):
    async def go():
        await _seed_theme()            # a1 (NOW-3h) + a2 (NOW-1h): restart killed the run
        await _pr(501, "Daemon restart no longer kills the verification run",
                  merged_at=NOW - timedelta(hours=2))
        await _pr(502, "Daemon restart killed verification run fix",       # merged before
                  merged_at=NOW - timedelta(hours=5))
        await _pr(503, "Bump eslint and vitest", merged_at=NOW - timedelta(hours=1))
        await _pr(504, "Daemon restart kills verification run", state="open",
                  merged_at=NOW - timedelta(hours=1))
        await _pr(505, "Daemon restart kills verification run", repo="other/repo",
                  merged_at=NOW - timedelta(hours=1))
        view = await _loop(_ctx(), FakeGh()).clusters()
        clusters = _by_members(view)
        hits = clusters[("a1", "a2")]["possiblyFixed"]
        assert [h["number"] for h in hits] == [501]
        assert hits[0]["title"] == "Daemon restart no longer kills the verification run"
        assert hits[0]["url"] == "https://github.com/o/r/pull/501"
        assert hits[0]["mergedAt"].startswith("2026-09-23T10:00:00")
        assert 0 < hits[0]["score"] <= 1
        assert clusters[("b1",)]["possiblyFixed"] == []
    _run(go, tmp_path)


def test_possibly_fixed_keeps_the_top_two(tmp_path):
    async def go():
        await _seed_theme()
        for n in (601, 602, 603):
            await _pr(n, f"Daemon restart killed the verification run (part {n})",
                      merged_at=NOW - timedelta(minutes=n - 590))
        hits = _by_members(await _loop(_ctx(), FakeGh()).clusters())[("a1", "a2")]["possiblyFixed"]
        assert len(hits) == 2
    _run(go, tmp_path)


# -- age-out --------------------------------------------------------------------

async def _seed_ages():
    old = NOW - timedelta(days=20)
    await _seed(id="lone", session_id="s1", fingerprint="fpLone", title="Kiosk clock drifts",
                reported_at=old)
    # Same fingerprint reported again recently, by another session.
    await _seed(id="fp_old", session_id="s2", fingerprint="fpR", title="Usage bar flickers",
                reported_at=old)
    await _seed(id="fp_new", session_id="s3", fingerprint="fpR", title="Usage bar flickers",
                reported_at=NOW - timedelta(days=1))
    # Same fingerprint recurred, and that recurrence was already filed.
    await _seed(id="filed_old", session_id="s4", fingerprint="fpF", title="Toast stacks forever",
                reported_at=old)
    await _seed(id="filed_new", session_id="s5", fingerprint="fpF", title="Toast stacks forever",
                status="filed", issue_number=7, reported_at=NOW - timedelta(days=2))
    # Different fingerprint, same problem (one cluster) — the recent one keeps it.
    await _seed(id="cl_old", session_id="s6", fingerprint="fpC1",
                title="Daemon restart killed the verification run", reported_at=old)
    await _seed(id="cl_new", session_id="s7", fingerprint="fpC2",
                title="Daemon restart killed a verification run again",
                reported_at=NOW - timedelta(days=1))


def test_age_out_archives_only_old_reports_that_never_recurred(tmp_path):
    async def go():
        await _seed_ages()
        gh = FakeGh()
        loop = _loop(_ctx(), gh, mode="off")             # runs while paused
        await loop.tick()
        lone = await _row("lone")
        assert lone.status == "archived"
        assert lone.resolution["kind"] == "aged_out" and lone.resolution["at"]
        for rid in ("fp_old", "fp_new", "filed_old", "cl_old", "cl_new"):
            assert (await _row(rid)).status == "pending", rid
        assert gh.calls == []
        # Never badged: not actionable, not in the clusters.
        assert "lone" not in {i.id for i in await loop.actionable_items()}
        view = await loop.clusters()
        assert all(m["id"] != "lone" for c in view["clusters"] for m in c["members"])
        assert view["digest"]["count"] == 1
        assert [i["id"] for i in view["digest"]["items"]] == ["lone"]
        assert view["digest"]["items"][0]["title"] == "Kiosk clock drifts"
    _run(go, tmp_path)


def test_age_out_zero_disables(tmp_path):
    async def go():
        await _seed_ages()
        loop = _loop(_ctx(), FakeGh(), mode="off", config={"age_out_days": 0})
        await loop.tick()
        assert (await _row("lone")).status == "pending"
        assert (await loop.clusters())["digest"] == {"count": 0, "items": []}
    _run(go, tmp_path)


def test_restore_brings_archived_reports_back_and_refuses_others(tmp_path):
    async def go():
        await _seed_ages()
        loop = _loop(_ctx(), FakeGh(), mode="off")
        await loop.tick()
        assert (await loop.restore(["fp_new"]))["status"] == "invalid_ids"   # not archived
        assert (await loop.restore([]))["status"] == "invalid_ids"
        r = await loop.restore(["lone"])
        assert r["status"] == "restored" and r["level"] == "ok" and r["count"] == 1
        row = await _row("lone")
        assert row.status == "pending" and row.resolution["kind"] == "restored"
        await loop.tick()                         # the operator's restore sticks
        assert (await _row("lone")).status == "pending"
        assert (await loop.clusters())["digest"]["count"] == 0
        # The provider seam carries it, and it counts as a write.
        orch = type("O", (), {})()
        orch._session_feedback_loop = loop
        p = SessionFeedbackActionProvider(orch)
        assert "restore" in p.valid_inbox_ops and "restore" in p.inbox_write_ops
    _run(go, tmp_path)


def test_counts_and_status_text_report_archived(tmp_path):
    async def go():
        await _seed_ages()
        loop = _loop(_ctx(), FakeGh(), mode="file", config={"min_impact": "blocked"})
        await loop.tick()
        counts = loop._ctx.session_feedback_counts
        assert counts["archived"] == 1
        text = SessionFeedbackFiler().status(loop._ctx).text
        assert "1 aged out" in text
    _run(go, tmp_path)


def test_config_schema_offers_age_out_and_session_link_base():
    fields = {f.key: f for f in SessionFeedbackFiler.config_schema}
    assert fields["age_out_days"].default == 14 and fields["age_out_days"].min == 0
    assert fields["session_link_base"].default == ""


# -- session links ----------------------------------------------------------------

def test_session_link_base_links_each_report_to_its_session(tmp_path):
    async def go():
        await _seed_theme()           # a1 carries its own session_url; a2 does not
        loop = _loop(_ctx(), FakeGh(), config={"session_link_base": "https://vp.example/"})
        body = (await loop.draft_cluster_issue(["a1", "a2"]))["body"]
        assert "[beta](https://vp.example/sessions/s2)" in body
        assert "[alpha](https://vp.example/sessions/s1)" in body
        plain = (await _loop(_ctx(), FakeGh()).draft_cluster_issue(["a2"]))["body"]
        assert "beta (`s2`)" in plain and "](" not in plain.split("| Session |")[1].split("\n")[0]
    _run(go, tmp_path)


def test_session_link_base_reaches_single_report_issues(tmp_path):
    async def go():
        await _seed(id="one", session_id="s1", context={"sessionId": "s1", "name": "solo"})
        gh = FakeGh()
        await _loop(_ctx(), gh, config={"session_link_base": "https://vp.example"}).tick()
        assert "[solo](https://vp.example/sessions/s1)" in gh.created_bodies[0]
    _run(go, tmp_path)


def test_a_non_https_link_base_is_ignored(tmp_path):
    async def go():
        await _seed(id="one", session_id="s1", context={"sessionId": "s1", "name": "solo"})
        loop = _loop(_ctx(), FakeGh(), config={"session_link_base": "javascript:alert(1)"})
        body = (await loop.draft_cluster_issue(["one"]))["body"]
        assert "javascript" not in body and "solo (`s1`)" in body
    _run(go, tmp_path)
