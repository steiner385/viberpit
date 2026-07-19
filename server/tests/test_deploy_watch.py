"""DeployWatcher unit tests (SPEC §3 — health polling, ancestry, qa/prod-live).

The watcher is exercised with injected ``http_get`` and ``gh_run`` so no real
network or gh subprocess runs. DB assertions use a per-test wipe of the two
tables the watcher touches (RepoDeployConfig, MergedPr).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from server.db import db_session, init_db
from server.db.models import MergedPr, RepoDeployConfig
from server.pipeline import timeline
from server.pipeline.deploy_watch import DeployWatcher, _DEPLOY_INTERVAL_S


@pytest.fixture(autouse=True)
def _clean_tables():
    async def wipe():
        await init_db()
        async with db_session() as db:
            await db.execute(delete(RepoDeployConfig))
            await db.execute(delete(MergedPr))
    asyncio.run(wipe())


# Relative anchor: always within the 7-day tracked_merged retention window.
_RECENT_MERGE = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
    "%Y-%m-%dT%H:%M:%SZ"
)


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #

def _resp(status: int, text: str):
    """Minimal httpx.Response-ish object: only .status_code and .text used."""
    return SimpleNamespace(status_code=status, text=text)


def _http_map(mapping: dict[str, object]):
    """Return an async http_get(url) that looks the response up by url.

    A value may be a response object or an Exception to raise.
    """
    async def _get(url: str):
        val = mapping.get(url)
        if isinstance(val, Exception):
            raise val
        if val is None:
            raise RuntimeError(f"unexpected url {url}")
        return val
    return _get


def _gh_status(status: str):
    """Fake gh_run returning a compare API JSON with the given status."""
    def _run(args):
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"status": status}), stderr=""
        )
    return _run


async def _seed_config(repo: str, environments: list[dict], enabled: bool = True):
    async with db_session() as db:
        db.add(RepoDeployConfig(
            repo=repo, environments=json.dumps(environments), enabled=enabled
        ))


async def _seed_merged(repo: str, number: int, *, merged_at: str, sha: str | None):
    await timeline.upsert_merged_pr(
        repo, number, merged_at=merged_at, merge_commit_sha=sha
    )


# --------------------------------------------------------------------------- #
# Health extraction                                                            #
# --------------------------------------------------------------------------- #

def test_health_extraction_default_key():
    async def go():
        await _seed_config("o/r", [
            {"name": "qa", "healthUrl": "http://qa.example/health", "auto": True}
        ])
        gets = _http_map({
            "http://qa.example/health": _resp(200, json.dumps({"commitSha": "abc123"})),
        })
        w = DeployWatcher(http_get=gets, gh_run=_gh_status("diverged"))
        await w.poll_once()
        st = w.status()
        env = st["o/r"][0]
        assert env["name"] == "qa"
        assert env["liveSha"] == "abc123"
        assert env["reachable"] is True
    asyncio.run(go())


def test_health_extraction_custom_sha_key():
    async def go():
        await _seed_config("o/r", [
            {"name": "prod", "healthUrl": "http://p.example/h", "auto": True,
             "shaKey": "gitSha"}
        ])
        gets = _http_map({
            "http://p.example/h": _resp(200, json.dumps({"gitSha": "deadbeef"})),
        })
        w = DeployWatcher(http_get=gets, gh_run=_gh_status("diverged"))
        await w.poll_once()
        assert w.status()["o/r"][0]["liveSha"] == "deadbeef"
    asyncio.run(go())


def test_health_non_string_value_is_none():
    async def go():
        await _seed_config("o/r", [
            {"name": "qa", "healthUrl": "http://qa/h", "auto": True}
        ])
        gets = _http_map({"http://qa/h": _resp(200, json.dumps({"commitSha": 123}))})
        w = DeployWatcher(http_get=gets, gh_run=_gh_status("diverged"))
        await w.poll_once()
        env = w.status()["o/r"][0]
        assert env["liveSha"] is None
        # plotroom invariant (deploy-status.ts:74): reachable == (liveSha != null)
        assert env["reachable"] is False
    asyncio.run(go())


def test_health_missing_key_is_none():
    async def go():
        await _seed_config("o/r", [
            {"name": "qa", "healthUrl": "http://qa/h", "auto": True}
        ])
        gets = _http_map({"http://qa/h": _resp(200, json.dumps({"other": "x"}))})
        w = DeployWatcher(http_get=gets, gh_run=_gh_status("diverged"))
        await w.poll_once()
        assert w.status()["o/r"][0]["liveSha"] is None
    asyncio.run(go())


def test_health_http_error_unreachable():
    async def go():
        await _seed_config("o/r", [
            {"name": "qa", "healthUrl": "http://qa/h", "auto": True}
        ])
        gets = _http_map({"http://qa/h": _resp(503, "down")})
        w = DeployWatcher(http_get=gets, gh_run=_gh_status("diverged"))
        await w.poll_once()
        env = w.status()["o/r"][0]
        assert env["liveSha"] is None
        assert env["reachable"] is False
    asyncio.run(go())


def test_health_exception_unreachable_never_raises():
    async def go():
        await _seed_config("o/r", [
            {"name": "qa", "healthUrl": "http://qa/h", "auto": True}
        ])
        gets = _http_map({"http://qa/h": ConnectionError("boom")})
        w = DeployWatcher(http_get=gets, gh_run=_gh_status("diverged"))
        await w.poll_once()  # must not raise
        env = w.status()["o/r"][0]
        assert env["liveSha"] is None
        assert env["reachable"] is False
    asyncio.run(go())


def test_health_bad_json_parse_error_is_none_false():
    async def go():
        await _seed_config("o/r", [
            {"name": "qa", "healthUrl": "http://qa/h", "auto": True}
        ])
        gets = _http_map({"http://qa/h": _resp(200, "not json {")})
        w = DeployWatcher(http_get=gets, gh_run=_gh_status("diverged"))
        await w.poll_once()
        env = w.status()["o/r"][0]
        # HTTP 200 but unparseable body: parse error -> (None, False) per brief.
        assert env["liveSha"] is None
        assert env["reachable"] is False
    asyncio.run(go())


def test_disabled_config_not_polled():
    async def go():
        await _seed_config("o/r", [
            {"name": "qa", "healthUrl": "http://qa/h", "auto": True}
        ], enabled=False)

        async def _get(url):
            raise AssertionError("disabled repo must not be polled")

        w = DeployWatcher(http_get=_get, gh_run=_gh_status("diverged"))
        await w.poll_once()
        assert w.status() == {}
    asyncio.run(go())


# --------------------------------------------------------------------------- #
# Ancestry direction — pinned against all four compare statuses               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("status,expect", [
    ("ahead", True),
    ("identical", True),
    ("behind", False),
    ("diverged", False),
])
def test_ancestry_direction(status, expect):
    async def go():
        w = DeployWatcher(http_get=_http_map({}), gh_run=_gh_status(status))
        result = await w._is_ancestor("o/r", "basesha", "headsha")
        assert result is expect
    asyncio.run(go())


def test_ancestry_gh_failure_is_false():
    async def go():
        def _run(args):
            return SimpleNamespace(returncode=1, stdout="", stderr="404")
        w = DeployWatcher(http_get=_http_map({}), gh_run=_run)
        assert await w._is_ancestor("o/r", "b", "h") is False
    asyncio.run(go())


def test_ancestry_throttle_checks_pair_once():
    async def go():
        calls = []

        def _run(args):
            calls.append(args)
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"status": "diverged"}), stderr=""
            )
        w = DeployWatcher(http_get=_http_map({}), gh_run=_run)
        assert await w._is_ancestor("o/r", "b", "h") is False
        assert await w._is_ancestor("o/r", "b", "h") is False  # throttled: no re-check
        assert len(calls) == 1
        # A NEW liveSha (new pair) re-checks.
        await w._is_ancestor("o/r", "b", "h2")
        assert len(calls) == 2
    asyncio.run(go())


def test_ancestry_yes_pair_checked_once_ever():
    """A pair is resolved at most once ever. The first probe returns the real
    answer; a re-probe of the SAME pair short-circuits to False (throttled) — the
    poll loop never re-probes since the PR's live column is already stamped."""
    async def go():
        calls = []

        def _run(args):
            calls.append(args)
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"status": "ahead"}), stderr=""
            )
        w = DeployWatcher(http_get=_http_map({}), gh_run=_run)
        assert await w._is_ancestor("o/r", "b", "h") is True
        assert await w._is_ancestor("o/r", "b", "h") is False  # throttled re-probe
        assert len(calls) == 1  # gh called once ever for the pair
    asyncio.run(go())


# --------------------------------------------------------------------------- #
# mark_env_live wiring (qa vs prod), full poll_once integration                #
# --------------------------------------------------------------------------- #

def test_poll_marks_qa_live_on_ancestor_yes():
    async def go():
        await _seed_config("o/r", [
            {"name": "qa", "healthUrl": "http://qa/h", "auto": True}
        ])
        await _seed_merged("o/r", 7, merged_at=_RECENT_MERGE, sha="prsha")
        gets = _http_map({"http://qa/h": _resp(200, json.dumps({"commitSha": "livesha"}))})
        w = DeployWatcher(http_get=gets, gh_run=_gh_status("ahead"))
        await w.poll_once()
        async with db_session() as db:
            row = (await db.execute(
                select(MergedPr).where(MergedPr.repo == "o/r", MergedPr.number == 7)
            )).scalar_one()
        assert row.qa_live_at is not None
        assert row.prod_live_at is None
    asyncio.run(go())


def test_poll_marks_prod_live_for_prod_env():
    async def go():
        await _seed_config("o/r", [
            {"name": "prod", "healthUrl": "http://p/h", "auto": True}
        ])
        await _seed_merged("o/r", 8, merged_at=_RECENT_MERGE, sha="prsha")
        gets = _http_map({"http://p/h": _resp(200, json.dumps({"commitSha": "livesha"}))})
        w = DeployWatcher(http_get=gets, gh_run=_gh_status("identical"))
        await w.poll_once()
        async with db_session() as db:
            row = (await db.execute(
                select(MergedPr).where(MergedPr.repo == "o/r", MergedPr.number == 8)
            )).scalar_one()
        assert row.prod_live_at is not None
        assert row.qa_live_at is None
    asyncio.run(go())


def test_poll_no_mark_when_not_ancestor():
    async def go():
        await _seed_config("o/r", [
            {"name": "qa", "healthUrl": "http://qa/h", "auto": True}
        ])
        await _seed_merged("o/r", 9, merged_at=_RECENT_MERGE, sha="prsha")
        gets = _http_map({"http://qa/h": _resp(200, json.dumps({"commitSha": "livesha"}))})
        w = DeployWatcher(http_get=gets, gh_run=_gh_status("diverged"))
        await w.poll_once()
        async with db_session() as db:
            row = (await db.execute(
                select(MergedPr).where(MergedPr.repo == "o/r", MergedPr.number == 9)
            )).scalar_one()
        assert row.qa_live_at is None
    asyncio.run(go())


def test_poll_skips_pr_with_column_already_set():
    """A PR already qa_live gets no ancestry check for the qa env."""
    async def go():
        await _seed_config("o/r", [
            {"name": "qa", "healthUrl": "http://qa/h", "auto": True}
        ])
        await timeline.upsert_merged_pr(
            "o/r", 10, merged_at=_RECENT_MERGE,
            merge_commit_sha="prsha", qa_live_at="2026-07-11T00:00:00Z",
        )
        gets = _http_map({"http://qa/h": _resp(200, json.dumps({"commitSha": "livesha"}))})
        calls = []

        def _run(args):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "ahead"}), stderr="")
        w = DeployWatcher(http_get=gets, gh_run=_run)
        await w.poll_once()
        assert calls == []  # qa_live already set -> no ancestry probe
    asyncio.run(go())


def test_poll_skips_pr_without_merge_sha():
    async def go():
        await _seed_config("o/r", [
            {"name": "qa", "healthUrl": "http://qa/h", "auto": True}
        ])
        await _seed_merged("o/r", 11, merged_at=_RECENT_MERGE, sha=None)
        gets = _http_map({"http://qa/h": _resp(200, json.dumps({"commitSha": "livesha"}))})
        calls = []

        def _run(args):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "ahead"}), stderr="")
        w = DeployWatcher(http_get=gets, gh_run=_run)
        await w.poll_once()
        assert calls == []
    asyncio.run(go())


def test_poll_skips_ancestry_when_livesha_none():
    """Unreachable env -> liveSha None -> no ancestry probe at all."""
    async def go():
        await _seed_config("o/r", [
            {"name": "qa", "healthUrl": "http://qa/h", "auto": True}
        ])
        await _seed_merged("o/r", 12, merged_at=_RECENT_MERGE, sha="prsha")
        gets = _http_map({"http://qa/h": _resp(503, "down")})
        calls = []

        def _run(args):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "ahead"}), stderr="")
        w = DeployWatcher(http_get=gets, gh_run=_run)
        await w.poll_once()
        assert calls == []
    asyncio.run(go())


# --------------------------------------------------------------------------- #
# status() shape and ordering                                                 #
# --------------------------------------------------------------------------- #

def test_status_shape_preserves_config_order():
    async def go():
        await _seed_config("o/r", [
            {"name": "qa", "healthUrl": "http://qa/h", "auto": True},
            {"name": "prod", "healthUrl": "http://p/h", "auto": False},
        ])
        gets = _http_map({
            "http://qa/h": _resp(200, json.dumps({"commitSha": "s1"})),
            "http://p/h": _resp(200, json.dumps({"commitSha": "s2"})),
        })
        w = DeployWatcher(http_get=gets, gh_run=_gh_status("diverged"))
        await w.poll_once()
        envs = w.status()["o/r"]
        assert [e["name"] for e in envs] == ["qa", "prod"]
        assert envs[0].keys() == {"name", "liveSha", "reachable"}
    asyncio.run(go())


def test_status_empty_before_poll():
    w = DeployWatcher(http_get=_http_map({}), gh_run=_gh_status("diverged"))
    assert w.status() == {}


def test_interval_constant():
    assert _DEPLOY_INTERVAL_S == 30.0


def test_url_guard_blocks_dangerous_targets_allows_tailnet():
    """SSRF guard: loopback/link-local(metadata)/reserved/multicast blocked;
    private + CGNAT (tailnet) and public hosts allowed; non-http schemes blocked."""
    from server.pipeline.deploy_watch import _url_allowed

    assert _url_allowed("http://127.0.0.1/health") is False
    assert _url_allowed("http://localhost:8000/health") is False
    assert _url_allowed("http://169.254.169.254/latest/meta-data/") is False
    assert _url_allowed("http://0.0.0.0/h") is False
    assert _url_allowed("http://224.0.0.1/h") is False
    assert _url_allowed("file:///etc/passwd") is False
    assert _url_allowed("http://10.0.0.5/health") is True          # private LAN
    assert _url_allowed("http://100.101.102.103/health") is True   # tailnet CGNAT
    assert _url_allowed("https://1.1.1.1/health") is True          # public
