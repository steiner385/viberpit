"""Live-boundary acceptance for the PM origins on a real Codex binary.

The half ``test_pm_codex_acceptance.py`` cannot certify from the schema
(constitution III): run a **bounded** classification batch and a **bounded**
map-synthesis + critic pair through the real ``codex`` binary, using the real
prompts ``server/pm/classification.py`` builds, and check that what comes back
still satisfies the contracts the orchestrator seams depend on.

Opt-in (``CCSWITCH_TEST_CODEX_LIVE=1``) because it spends OpenAI quota and takes
minutes. CI never runs it; the recorded evidence lives in
``docs/product/features/product-management-layer.md``.

**What each half proves.**

* *Classification* is a pure text contract — a fenced JSON array in the final
  message — so it needs no repository. The proof is that
  ``parse_classification_output`` (the exact parser ``_pm_classify_ingest``
  runs) yields one row per listed item and nothing for an item that was not
  listed. ``-s read-only`` is deliberate: a classify session has no write to do,
  and running it in the narrowest profile shows the harvest does not depend on
  the escalated sandbox the map half needs.

* *Map synthesis + critic* is the propose-only chain, and the property under
  test is **review separation**: synthesis pushes ONE branch and never touches
  the default branch, and the critic augments THAT SAME branch rather than
  opening a second one. That is checkable without GitHub — the harness's
  ``origin`` is a local bare repository — and it is the half a mis-wired engine
  would break silently. The diff-scoped prompt is used rather than the whole-repo
  archaeology one so the run stays bounded; it is the same builder, same
  requirements block, same trailer.

Not covered, deliberately: ``gh pr create``. It needs GitHub and is plain shell,
identical on either engine — the non-draft / no-auto-merge PR attributes stay
prompt-level guarantees asserted in ``test_pm_codex_acceptance.py``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ccswitch.engines import AgentEngine
from server.pm.classification import (
    FEATURE_DOCS_DIR,
    FeatureBrief,
    WorkItemBrief,
    build_classification_prompt,
    build_coverage_critic_prompt,
    build_map_synthesis_prompt,
    parse_classification_output,
)
from server.sessions.completion import commit_trailer

pytestmark = pytest.mark.skipif(
    os.environ.get("CCSWITCH_TEST_CODEX_LIVE") != "1" or shutil.which("codex") is None,
    reason="live Codex boundary test: set CCSWITCH_TEST_CODEX_LIVE=1 with a codex binary on PATH",
)

SLUG = "codex-live-pm"
DEFAULT_BRANCH = "main"
_TIMEOUT_S = 900

# The map/critic sessions run in the profile #1263's opt-in escalates a
# `full-auto` automation launch to; anything narrower is network-isolated and
# could not push (`server/codex/permissions.py`).
_WRITE_SANDBOX = "danger-full-access"

#: Appended to the map/critic prompts, fenced and labelled. The harness `origin`
#: is a local bare repository with no PR service; pretending otherwise would only
#: buy a `gh` failure loop.
_HARNESS_NOTE = """

--- TEST HARNESS NOTE (not part of the PM contract) ---
This repository's `origin` is a local bare git repository, not GitHub. There is
no PR service reachable here, so perform the pull-request requirement as far as
it can go — push the branch to `origin` — and then stop. Do not attempt `gh`.
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(("git", *args), cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


def _codex(cwd: Path, prompt: str, *, sandbox: str) -> str:
    result = subprocess.run(
        ("codex", "exec", "-s", sandbox, "-C", str(cwd), "--skip-git-repo-check", "-"),
        input=prompt, text=True, capture_output=True, timeout=_TIMEOUT_S)
    assert result.returncode == 0, f"codex exec failed:\n{result.stdout[-4000:]}"
    return result.stdout


# --------------------------------------------------------------------------- #
# 1. A bounded classification batch (#1269).                                   #
# --------------------------------------------------------------------------- #

ITEMS = [
    WorkItemBrief(number=101, kind="issue", title="Session list never refreshes",
                  body="The sessions page shows stale rows until a manual reload.",
                  labels=("bug",), url=""),
    WorkItemBrief(number=102, kind="issue", title="Add a keyboard shortcut to switch accounts",
                  body="Switching accounts needs three clicks today.",
                  labels=("enhancement",), url=""),
    WorkItemBrief(number=103, kind="issue", title="Publish a Grafana dashboard for queue depth",
                  body="Nothing in the product covers operational dashboards yet.",
                  labels=(), url=""),
]
FEATURES = [
    FeatureBrief(slug="sessions", title="Session management",
                 summary="Listing, attaching to and switching between live agent sessions."),
    FeatureBrief(slug="accounts", title="Account switching",
                 summary="Managing several engine-scoped accounts and moving between them."),
]


def test_a_bounded_classification_batch_parses_into_proposals(tmp_path):
    """The deliverable is a fenced JSON array the ingest parser consumes. What is
    asserted is the CONTRACT (one row per listed item, no invented items, the
    no_fit escape hatch available), not which feature the model picked — that is
    a judgement a human accepts or rejects in the Triage Inbox."""
    prompt = build_classification_prompt(items=ITEMS, features=FEATURES)
    output = _codex(tmp_path, prompt, sandbox="read-only")

    parsed = parse_classification_output(output)
    numbers = [p.number for p in parsed]
    assert sorted(numbers) == [101, 102, 103], (
        f"expected one classification per listed item, got {numbers}\n"
        f"{output[-4000:]}")
    assert len(numbers) == len(set(numbers)), "an item was classified twice"
    for p in parsed:
        # Every row is either an assignment to an OFFERED feature or an explicit
        # no_fit carrying its suggestion — the two answers the seam maps onto a
        # TriageProposal. Anything else would be silently dropped at ingest.
        if p.feature_slug is None:
            assert p.no_fit_suggestion, f"#{p.number}: no_fit with no suggestion"
        else:
            assert p.feature_slug in {f.slug for f in FEATURES}, (
                f"#{p.number}: invented feature {p.feature_slug!r}")
        assert p.confidence_level in ("low", "medium", "high")


# --------------------------------------------------------------------------- #
# 2. Bounded map synthesis + critic, propose-only (#1268).                     #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> tuple[Path, Path]:
    """A throwaway work tree plus the local bare repo standing in for GitHub."""
    root = tmp_path_factory.mktemp("codex-pm-live")
    work, remote = root / "work", root / "remote.git"
    remote.mkdir()
    subprocess.run(("git", "init", "--bare", "-q", "-b", DEFAULT_BRANCH, str(remote)),
                   check=True)
    work.mkdir()
    _git(work, "init", "-q", "-b", DEFAULT_BRANCH)
    _git(work, "config", "user.email", "pm-live@example.invalid")
    _git(work, "config", "user.name", "PM Live Harness")
    (work / "README.md").write_text(
        "# disposable acceptance repo\n\nA tiny service with a queue worker and "
        "an HTTP API.\n")
    (work / "api.py").write_text("def handle(request):\n    return {'ok': True}\n")
    (work / "worker.py").write_text("def drain(queue):\n    return len(queue)\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", DEFAULT_BRANCH)
    _git(work, "fetch", "-q", "origin")
    return work, remote


def _pushed_branches(remote: Path) -> list[str]:
    heads = [line.split("refs/heads/")[-1]
             for line in _git(remote, "for-each-ref", "--format=%(refname)",
                              "refs/heads/").splitlines()]
    return [h for h in heads if h != DEFAULT_BRANCH]


def test_map_synthesis_and_its_critic_share_one_proposed_branch(repo):
    """The propose-only chain, live: synthesis proposes on ONE branch and the
    critic augments the SAME one — review separation is the whole gate, and a
    second branch (or a commit on the default branch) would break it.

    One test because the critic is seeded from the branch synthesis pushed;
    splitting them would either re-run synthesis or hide it behind a fixture.
    """
    work, remote = repo
    _git(work, "checkout", "-q", DEFAULT_BRANCH)
    _git(work, "reset", "-q", "--hard", f"origin/{DEFAULT_BRANCH}")
    default_head_before = _git(work, "rev-parse", f"origin/{DEFAULT_BRANCH}")

    _codex(work, build_map_synthesis_prompt(
        slug=SLUG, repo="local/disposable", default_branch=DEFAULT_BRANCH,
        open_count=2, closed_count=0, scope="diff", suggestion="queue-worker",
        item_numbers=(1, 2), engine=AgentEngine.CODEX) + _HARNESS_NOTE,
        sandbox=_WRITE_SANDBOX)

    branches = _pushed_branches(remote)
    assert len(branches) == 1, f"synthesis must propose ONE branch, got {branches}"
    branch = branches[0]
    _git(work, "fetch", "-q", "origin")
    assert _git(work, "rev-parse", f"origin/{DEFAULT_BRANCH}") == default_head_before, (
        "synthesis committed to the default branch — the human merge is the gate")

    changed = _git(work, "diff", "--name-only",
                   f"origin/{DEFAULT_BRANCH}...origin/{branch}").splitlines()
    assert changed, "synthesis proposed nothing"
    assert all(p.startswith(f"{FEATURE_DOCS_DIR}/") for p in changed), (
        f"synthesis wrote outside the feature-doc directory: {changed}")
    messages = _git(work, "log", "--format=%B",
                    f"origin/{DEFAULT_BRANCH}..origin/{branch}")
    assert commit_trailer(AgentEngine.CODEX) in messages
    assert commit_trailer(AgentEngine.CLAUDE_CODE) not in messages
    synth_head = _git(work, "rev-parse", f"origin/{branch}")

    # ---- The critic: a FRESH session on the same branch. ------------------- #
    _git(work, "checkout", "-q", "-B", branch, f"origin/{branch}")
    _codex(work, build_coverage_critic_prompt(
        slug=SLUG, repo="local/disposable", default_branch=DEFAULT_BRANCH,
        branch=branch, open_count=2, closed_count=0,
        engine=AgentEngine.CODEX) + _HARNESS_NOTE, sandbox=_WRITE_SANDBOX)

    _git(work, "fetch", "-q", "origin")
    assert _pushed_branches(remote) == [branch], (
        "the critic opened a second branch instead of augmenting the map PR")
    assert _git(work, "rev-parse", f"origin/{DEFAULT_BRANCH}") == default_head_before
    critic_messages = _git(work, "log", "--format=%B",
                           f"{synth_head}..origin/{branch}")
    if critic_messages.strip():  # a critic that finds no gap correctly adds nothing
        assert commit_trailer(AgentEngine.CODEX) in critic_messages
        assert commit_trailer(AgentEngine.CLAUDE_CODE) not in critic_messages
        critic_changed = _git(work, "diff", "--name-only",
                              f"{synth_head}..origin/{branch}").splitlines()
        assert any(p.startswith(f"{FEATURE_DOCS_DIR}/") for p in critic_changed), (
            f"the critic committed but added no feature doc: {critic_changed}")
        # Documentation only — the critic must never touch source. Its paths are
        # NOT pinned to `FEATURE_DOCS_DIR` the way synthesis's are: the prompt
        # routes "genuinely not a feature" findings to the PR's coverage-audit
        # table, and this harness's `origin` is a bare repo with no PR body, so a
        # live critic reasonably writes that table to a file under `docs/`
        # instead. That is the harness's missing PR, not a contract breach.
        assert all(p.startswith("docs/") for p in critic_changed), (
            f"the critic wrote outside docs/: {critic_changed}")
