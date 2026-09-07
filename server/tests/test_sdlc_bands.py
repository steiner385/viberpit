"""``bands.yaml`` schema/loader tests + a validation pass over the COMMITTED policy (#804).

The loader's contract is that a malformed policy raises rather than half-loads: a
monitor that silently dropped an unparseable band would present as "nothing ever
breached", which is indistinguishable from a healthy system.
"""
from __future__ import annotations

import textwrap

import pytest

from server.sdlc.bands import (
    DEFAULT_BANDS_PATH,
    RESPONSES,
    BandsConfigError,
    load_bands,
    load_bands_text,
)
from server.sdlc.control_bands import METRIC_SOURCES
from server.sessions.types import VALID_SESSION_MODES

_VALID = textwrap.dedent("""
    version: 1
    tiers:
      log:
        sigma: 1
        response: log
        tools: []
      diagnose:
        sigma: 2
        response: diagnose
        permission_mode: full-auto
        tools: [Read, Grep]
      propose:
        sigma: 3
        response: propose
        tools: [Read, Write, Bash]
    metrics:
      - id: cycle-time
        source: pr_cycle_time_hours
        repo: steiner385/ccswitch
        title: PR cycle time
        unit: hours
        direction: higher_is_worse
        baseline_points: 30
        min_baseline: 10
""")


def _with_metric(entry: str) -> str:
    """``_VALID`` plus one more metric. The entry is appended at the list's own
    indentation, so it must be written flush-left here and is re-indented."""
    return _VALID + textwrap.indent(textwrap.dedent(entry).strip("\n"), "  ") + "\n"


# --------------------------------------------------------------------------- #
# Happy path                                                                    #
# --------------------------------------------------------------------------- #

def test_loads_tiers_sorted_by_sigma_with_their_tool_allowlists():
    cfg = load_bands_text(_VALID)
    assert [t.sigma for t in cfg.tiers] == [1, 2, 3]
    assert [t.name for t in cfg.tiers] == ["log", "diagnose", "propose"]
    assert [t.response for t in cfg.tiers] == list(RESPONSES)
    assert cfg.tiers[1].tools == ("Read", "Grep")
    # An unspecified permission_mode defaults to the non-interactive fallback.
    assert cfg.tiers[2].permission_mode == "full-auto"


def test_loads_metrics_with_defaults_applied():
    cfg = load_bands_text(_VALID)
    (m,) = cfg.metrics
    assert (m.id, m.source, m.repo) == ("cycle-time", "pr_cycle_time_hours",
                                        "steiner385/ccswitch")
    assert m.enabled is True
    assert cfg.metric("cycle-time") is m
    assert cfg.metric("nope") is None


def test_disabled_metrics_are_parsed_but_excluded_from_enabled_metrics():
    cfg = load_bands_text(_with_metric("""
        - id: ci-fail
          source: ci_failure_rate
          repo: steiner385/ccswitch
          enabled: false
    """))
    assert len(cfg.metrics) == 2
    assert [m.id for m in cfg.enabled_metrics()] == ["cycle-time"]


# --------------------------------------------------------------------------- #
# tier_for_sigma                                                                #
# --------------------------------------------------------------------------- #

def test_tier_for_sigma_selects_the_matching_tier():
    cfg = load_bands_text(_VALID)
    assert cfg.tier_for_sigma(1).name == "log"
    assert cfg.tier_for_sigma(2).name == "diagnose"
    assert cfg.tier_for_sigma(3).name == "propose"


def test_tier_for_sigma_falls_back_to_the_highest_tier_at_or_below():
    """A configuration gap must never downgrade a real signal to 'ignore'."""
    cfg = load_bands_text(textwrap.dedent("""
        version: 1
        tiers:
          log: {sigma: 1, response: log}
          diagnose: {sigma: 2, response: diagnose}
        metrics: []
    """))
    assert cfg.tier_for_sigma(3).name == "diagnose"
    assert cfg.tier_for_sigma(5).name == "diagnose"


def test_tier_for_sigma_is_none_below_every_declared_tier():
    cfg = load_bands_text(textwrap.dedent("""
        version: 1
        tiers:
          propose: {sigma: 3, response: propose}
        metrics: []
    """))
    assert cfg.tier_for_sigma(1) is None


# --------------------------------------------------------------------------- #
# Validation refusals                                                           #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text, match", [
    ("version: 2\ntiers: {log: {sigma: 1, response: log}}\n", "unsupported bands.yaml version"),
    ("tiers: {log: {sigma: 1, response: log}}\n", "version must be an integer"),
    ("version: 1\ntiers: {}\n", "at least one response tier"),
    ("version: 1\ntiers: []\n", "tiers must be a mapping"),
    ("version: 1\ntiers: {a: {sigma: 1, response: nope}}\n", "response must be one of"),
    ("version: 1\ntiers: {a: {sigma: 0, response: log}}\n", "sigma must be >= 1"),
    ("version: 1\ntiers: {a: {sigma: true, response: log}}\n", "sigma must be an integer"),
    ("version: 1\ntiers: {a: {sigma: 1, response: log}, b: {sigma: 1, response: diagnose}}\n",
     "share a sigma"),
    ("version: 1\ntiers: {a: {sigma: 1, response: log, tools: [Read]}}\n",
     "tools list must be empty"),
    ("version: 1\ntiers: {a: {sigma: 1, response: log, tools: 'Read'}}\n",
     "must be a list of strings"),
])
def test_malformed_tiers_raise(text, match):
    with pytest.raises(BandsConfigError, match=match):
        load_bands_text(text)


def test_a_spawning_tier_may_not_be_interactive():
    """An unattended session in `interactive` blocks forever on a permission
    prompt nobody will answer (#750)."""
    with pytest.raises(BandsConfigError, match="block forever on interactive"):
        load_bands_text(textwrap.dedent("""
            version: 1
            tiers:
              d: {sigma: 2, response: diagnose, permission_mode: interactive}
            metrics: []
        """))


def test_an_unknown_permission_mode_is_rejected():
    with pytest.raises(BandsConfigError, match="not a valid session mode"):
        load_bands_text(textwrap.dedent("""
            version: 1
            tiers:
              d: {sigma: 2, response: diagnose, permission_mode: yolo}
            metrics: []
        """))


@pytest.mark.parametrize("metric, match", [
    ("{id: x, source: nope, repo: a/b}", "source must be one of"),
    ("{id: x, source: pr_cycle_time_hours, repo: nope}", "must be an 'owner/name'"),
    ("{id: '', source: pr_cycle_time_hours, repo: a/b}", "id must be a non-empty string"),
    ("{id: x, source: pr_cycle_time_hours, repo: a/b, direction: up}", "direction must be one of"),
    ("{id: x, source: pr_cycle_time_hours, repo: a/b, min_baseline: 1}", "min_baseline must be >= 2"),
    ("{id: x, source: pr_cycle_time_hours, repo: a/b, baseline_points: 5, min_baseline: 10}",
     "is below min_baseline"),
    ("{id: x, source: pr_cycle_time_hours, repo: a/b, enabled: yes-please}",
     "enabled must be a boolean"),
])
def test_malformed_metrics_raise(metric, match):
    text = (f"version: 1\ntiers: {{log: {{sigma: 1, response: log}}}}\n"
            f"metrics: [{metric}]\n")
    with pytest.raises(BandsConfigError, match=match):
        load_bands_text(text)


def test_duplicate_metric_ids_are_rejected():
    entry = "{id: x, source: pr_cycle_time_hours, repo: a/b}"
    text = (f"version: 1\ntiers: {{log: {{sigma: 1, response: log}}}}\n"
            f"metrics: [{entry}, {entry}]\n")
    with pytest.raises(BandsConfigError, match="duplicate metric id"):
        load_bands_text(text)


def test_invalid_yaml_raises_a_bands_error_not_a_yaml_error():
    with pytest.raises(BandsConfigError, match="not valid YAML"):
        load_bands_text("version: 1\ntiers: {oops\n")


def test_a_missing_file_raises_a_bands_error():
    with pytest.raises(BandsConfigError, match="cannot read bands config"):
        load_bands("/nonexistent/bands.yaml")


# --------------------------------------------------------------------------- #
# The COMMITTED policy — this is the gate on server/sdlc/bands.yaml itself      #
# --------------------------------------------------------------------------- #

def test_the_committed_bands_policy_loads():
    cfg = load_bands()
    assert DEFAULT_BANDS_PATH.exists()
    assert cfg.version == 1
    assert cfg.metrics, "the shipped policy must watch at least one metric"
    assert cfg.enabled_metrics(), "at least one shipped metric must be enabled"


def test_the_committed_policy_declares_the_three_designed_tiers():
    """Design §3.2 / issue #804: 1σ log · 2σ diagnose (read-only) · 3σ propose."""
    cfg = load_bands()
    by_sigma = {t.sigma: t for t in cfg.tiers}
    assert set(by_sigma) == {1, 2, 3}
    assert by_sigma[1].response == "log"
    assert by_sigma[2].response == "diagnose"
    assert by_sigma[3].response == "propose"


def test_the_committed_diagnose_tier_grants_no_mutating_tool():
    """The 2σ tier is read-only by design; a bare `Bash`, `Write` or `Edit` in its
    allowlist would contradict the prompt that tells the session not to change
    anything."""
    cfg = load_bands()
    (tier,) = [t for t in cfg.tiers if t.response == "diagnose"]
    assert tier.tools, "a diagnose tier with no tools would spawn a session that can do nothing"
    for tool in tier.tools:
        assert tool not in ("Bash", "Write", "Edit", "NotebookEdit"), tool
        assert not tool.startswith(("Write(", "Edit("))


def test_every_committed_metric_names_a_real_source_and_a_sane_mode():
    cfg = load_bands()
    for m in cfg.metrics:
        assert m.source in METRIC_SOURCES
    for t in cfg.tiers:
        assert t.permission_mode in VALID_SESSION_MODES
