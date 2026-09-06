"""Live-boundary acceptance for the SDLC artifact chain on a real Codex binary.

This is the half of the Task 7.3 gate that `test_sdlc_codex_acceptance.py` says
it cannot certify: *run one initiative through intent → design → plan using
Codex, keeping every human gate intact*. Constitution III wants that verified
against the real binary, not asserted from the schema, so this drives
``codex exec`` — the same binary the daemon runs — over the **real** prompts
``server/sdlc/prompts.py`` builds.

It is opt-in (``CCSWITCH_TEST_CODEX_LIVE=1``) because it costs an OpenAI account
real quota and takes minutes. CI never runs it; the recorded evidence lives in
``docs/product/features/openai-codex-integration.md``.

**What the fixture reproduces, and what it deliberately does not.** The
initiative runs in a throwaway repository whose ``origin`` is a local *bare*
repository, so every step an artifact session performs — branch off
``origin/<default>``, write one file, commit with the engine's trailer, push —
is exercised for real with no network and nothing outward-facing. The final
``gh pr create`` is not: it needs GitHub, and it is the one step in the contract
that is pure shell and identical on either engine. A short, fenced harness note
tells the session to stop after the push; nothing else in the prompt is altered.

**The human gate is the fixture's structure, not an assertion about it.** Stage
N+1 is seeded from stage N's artifact as committed on the default branch, and
this harness merges it there by hand between stages. That merge *is* the human
advance tap. If a stage could advance the chain on its own, the next stage would
find its upstream already present without the harness ever merging — so the gate
is load-bearing here rather than described.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ccswitch.engines import AgentEngine
from server.sdlc.prompts import artifact_path, build_intent_prompt, build_plan_prompt, build_spec_prompt
from server.sessions.completion import commit_trailer

pytestmark = pytest.mark.skipif(
    os.environ.get("CCSWITCH_TEST_CODEX_LIVE") != "1" or shutil.which("codex") is None,
    reason="live Codex boundary test: set CCSWITCH_TEST_CODEX_LIVE=1 with a codex binary on PATH",
)

SLUG = "codex-live-acceptance"
DEFAULT_BRANCH = "main"
TRIGGER = ("Operators cannot tell which agent engine produced an SDLC artifact, "
           "so a mixed-engine initiative is impossible to audit after the fact.")

#: Appended to each real prompt, fenced and labelled. See the module docstring:
#: the local bare `origin` has no PR service, and pretending otherwise would only
#: buy a `gh` failure loop.
_HARNESS_NOTE = """

--- TEST HARNESS NOTE (not part of the SDLC contract) ---
This repository's `origin` is a local bare git repository, not GitHub. There is
no PR service reachable here, so perform the pull-request requirement as far as
it can go — push the branch to `origin` — and then stop. Do not attempt `gh`.
"""

# The session runs unattended in the profile #1263's opt-in escalates a
# `full-auto` automation launch to; anything narrower is network-isolated and
# could not push (`server/codex/permissions.py`).
_SANDBOX = "danger-full-access"
_STAGE_TIMEOUT_S = 900


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(("git", *args), cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> tuple[Path, Path]:
    """A throwaway work tree plus the local bare repo standing in for GitHub."""
    root = tmp_path_factory.mktemp("codex-sdlc-live")
    work, remote = root / "work", root / "remote.git"
    remote.mkdir()
    subprocess.run(("git", "init", "--bare", "-q", "-b", DEFAULT_BRANCH, str(remote)),
                   check=True)
    work.mkdir()
    _git(work, "init", "-q", "-b", DEFAULT_BRANCH)
    _git(work, "config", "user.email", "sdlc-live@example.invalid")
    _git(work, "config", "user.name", "SDLC Live Harness")
    (work / "README.md").write_text("# disposable acceptance repo\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", DEFAULT_BRANCH)
    _git(work, "fetch", "-q", "origin")
    return work, remote


def _run_stage(work: Path, prompt: str) -> None:
    """Drive one artifact session on the real binary, from a clean default branch."""
    _git(work, "checkout", "-q", DEFAULT_BRANCH)
    _git(work, "fetch", "-q", "origin")
    _git(work, "reset", "-q", "--hard", f"origin/{DEFAULT_BRANCH}")
    result = subprocess.run(
        ("codex", "exec", "-s", _SANDBOX, "-C", str(work), "--skip-git-repo-check", "-"),
        input=prompt + _HARNESS_NOTE, text=True, capture_output=True,
        timeout=_STAGE_TIMEOUT_S)
    assert result.returncode == 0, f"codex exec failed:\n{result.stdout[-4000:]}"


def _pushed_branch(remote: Path) -> str:
    """The one non-default branch the stage pushed. Its existence IS the proof
    that a `danger-full-access` Codex session completes the push half of an
    automation's contract — the step `unattended_launch_blocker` refuses any
    narrower profile for."""
    heads = [line.split("refs/heads/")[-1]
             for line in _git(remote, "for-each-ref", "--format=%(refname)",
                              "refs/heads/").splitlines()]
    branches = [h for h in heads if h != DEFAULT_BRANCH]
    assert len(branches) == 1, f"expected exactly one proposed branch, got {branches}"
    return branches[0]


def _advance(work: Path, remote: Path, branch: str) -> None:
    """The human advance tap: merge the proposed artifact into the default branch.

    Nothing in the chain does this on its own — that is the propose-only gate.
    """
    _git(work, "fetch", "-q", "origin")
    _git(work, "checkout", "-q", DEFAULT_BRANCH)
    _git(work, "merge", "-q", "--no-ff", "-m", f"advance {branch}", f"origin/{branch}")
    _git(work, "push", "-q", "origin", DEFAULT_BRANCH)
    _git(remote, "update-ref", "-d", f"refs/heads/{branch}")


def _assert_stage_contract(work: Path, remote: Path, kind: str) -> str:
    """Every propose-only guarantee that is checkable without GitHub."""
    branch = _pushed_branch(remote)
    _git(work, "fetch", "-q", "origin")
    base = f"origin/{DEFAULT_BRANCH}"
    changed = _git(work, "diff", "--name-only", f"{base}...origin/{branch}").splitlines()

    assert changed == [artifact_path(SLUG, kind)], (
        f"a {kind} stage must add exactly its own artifact; it changed {changed}")
    body = _git(work, "show", f"origin/{branch}:{artifact_path(SLUG, kind)}")
    assert body.strip(), "the artifact is empty"

    messages = _git(work, "log", "--format=%B", f"{base}..origin/{branch}")
    assert commit_trailer(AgentEngine.CODEX) in messages, (
        "the Codex commit trailer is missing — the one engine-specific line in "
        f"the prompt did not survive the live run:\n{messages}")
    assert commit_trailer(AgentEngine.CLAUDE_CODE) not in messages
    assert branch != DEFAULT_BRANCH
    return branch


def test_the_codex_sdlc_chain_produces_each_artifact_behind_its_human_gate(repo):
    """intent → (human advance) → spec → (human advance) → plan, live.

    Asserted as one test because the stages are one sequence: each is seeded from
    its predecessor's *committed* artifact, so splitting them would either
    re-run the chain three times or hide a shared fixture behind three names.
    """
    work, remote = repo

    # ---- Stage 1: intake. A trigger becomes intent.md. --------------------- #
    _run_stage(work, build_intent_prompt(
        TRIGGER, slug=SLUG, default_branch=DEFAULT_BRANCH, engine=AgentEngine.CODEX))
    intent_branch = _assert_stage_contract(work, remote, "intent")

    # The gate: until a human merges, the next stage has no upstream to read.
    assert artifact_path(SLUG, "intent") not in _git(
        work, "ls-tree", "-r", "--name-only", f"origin/{DEFAULT_BRANCH}"), (
        "the intent artifact reached the default branch without a human advance")
    _advance(work, remote, intent_branch)
    intent_md = _git(work, "show", f"origin/{DEFAULT_BRANCH}:{artifact_path(SLUG, 'intent')}")

    # ---- Stage 2: design. intent.md becomes spec.md. ----------------------- #
    _run_stage(work, build_spec_prompt(
        intent_md, [], slug=SLUG, default_branch=DEFAULT_BRANCH,
        engine=AgentEngine.CODEX))
    spec_branch = _assert_stage_contract(work, remote, "spec")
    # Requirement 5: the upstream artifact is a read-only input.
    assert _git(work, "show", f"origin/{spec_branch}:{artifact_path(SLUG, 'intent')}") \
        == intent_md, "the design stage edited its upstream intent.md"
    _advance(work, remote, spec_branch)
    spec_md = _git(work, "show", f"origin/{DEFAULT_BRANCH}:{artifact_path(SLUG, 'spec')}")

    # ---- Stage 3: plan. spec.md becomes plan.md. --------------------------- #
    _run_stage(work, build_plan_prompt(
        spec_md, slug=SLUG, default_branch=DEFAULT_BRANCH, engine=AgentEngine.CODEX))
    plan_branch = _assert_stage_contract(work, remote, "plan")
    for upstream in ("intent", "spec"):
        assert _git(work, "show",
                    f"origin/{plan_branch}:{artifact_path(SLUG, upstream)}") == (
            intent_md if upstream == "intent" else spec_md), (
            f"the plan stage edited its upstream {upstream}.md")
