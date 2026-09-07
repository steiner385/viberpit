"""``bands.yaml`` schema + loader — the version-controlled control-band policy.

Phase 2 of the AI-Native SDLC design (issue #804). ``server/sdlc/bands.yaml`` is
the *policy* half of the Maintain loop: which metrics ccswitch watches, and what
it is allowed to do at each σ of breach. It is committed, reviewed and diffable
for the same reason the constitution is — a change to what an unattended agent may
do in response to a production signal is a governance change, not a setting.

This module is **pure**: it parses and validates text into frozen dataclasses. The
only filesystem touch is ``load_bands``' convenience read of the packaged file, and
even that funnels into ``load_bands_text`` so tests never need a temp file.

Response tiers (design §3.2, issue #804): *1σ log · 2σ diagnose (read-only) · 3σ
propose*. Each tier declares the tools its response session may use.

A note on how ``tools`` is enforced
-----------------------------------
ccswitch's ``create_session`` has no per-session tool allowlist — the SDK's
``allowed_tools`` is only reachable on the one-shot query path
(``nudge_composer``, ``autopilot_decider``), which cannot host a repo
investigation. A tier's ``tools`` list is therefore a **prompt-level binding
constraint**: the monitor states it verbatim in the response session's seed
prompt. That is a real constraint on a cooperative agent and it is honest about
what it is; the *hard* limits on the same session are the ones ccswitch actually
enforces — its ``permission_mode``, the managed deny rules in
``.claude/managed-settings.json`` (``server.sdlc.governance.MANAGED_DENY``), and
the prod-deploy gate hook. Do not read ``tools`` as a sandbox.

``permission_mode`` IS enforced, and the loader rejects ``interactive`` for any
tier that spawns: an unattended session that asks a human for permission blocks
forever on a prompt nobody will answer (#750). That check is why the mode belongs
in this validated schema rather than in a free-form config blob.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from server.sdlc.control_bands import DIRECTIONS, METRIC_SOURCES
from server.sessions.types import VALID_SESSION_MODES

#: The packaged policy file. Committed next to this module so the config and the
#: code that validates it move together in one reviewable diff.
DEFAULT_BANDS_PATH = Path(__file__).with_name("bands.yaml")

#: What a tier may do about a breach, least to most invasive. ``log`` records
#: only; ``diagnose`` spawns a read-only investigation session; ``propose`` files
#: the incident into the triage queue, from which SDLC Intake produces an
#: ``intent.md`` PR — the step that closes the loop back into Phase 1.
RESPONSES: tuple[str, ...] = ("log", "diagnose", "propose")

#: Permission modes a tier may name. ``interactive`` is excluded on purpose (see
#: the module docstring); the rest are the real ``VALID_SESSION_MODES``.
_SPAWNING_RESPONSES = frozenset({"diagnose", "propose"})


class BandsConfigError(ValueError):
    """``bands.yaml`` is malformed. Raised with a message naming the offending
    key, because a control-band policy that silently half-loads is worse than one
    that refuses to load at all."""


@dataclass(frozen=True)
class BandTier:
    """One response tier. ``sigma`` is the zone that selects it; ``response`` is
    what ccswitch does; ``tools`` is the prompt-level allowlist for the session
    that response spawns (empty for ``log``, which spawns nothing)."""
    name: str
    sigma: int
    response: str
    tools: tuple[str, ...] = ()
    permission_mode: str = "full-auto"


@dataclass(frozen=True)
class MetricBand:
    """One watched metric. ``source`` names the series reader (see
    ``control_bands.METRIC_SOURCES``); ``repo`` scopes it, since every source
    today is per-repository."""
    id: str
    source: str
    repo: str
    title: str = ""
    unit: str = ""
    direction: str = "higher_is_worse"
    baseline_points: int = 30
    min_baseline: int = 10
    enabled: bool = True


@dataclass(frozen=True)
class BandsConfig:
    """The whole parsed policy. ``tiers`` is ordered by ascending σ."""
    version: int
    tiers: tuple[BandTier, ...]
    metrics: tuple[MetricBand, ...]

    def enabled_metrics(self) -> tuple[MetricBand, ...]:
        return tuple(m for m in self.metrics if m.enabled)

    def metric(self, metric_id: str) -> MetricBand | None:
        for m in self.metrics:
            if m.id == metric_id:
                return m
        return None

    def tier_for_sigma(self, sigma: int) -> BandTier | None:
        """The tier governing a breach at *sigma* — the highest-σ tier at or below
        it. A 3σ breach under a policy that only declares 1σ and 2σ tiers is
        answered by the 2σ tier rather than going unanswered: a configuration gap
        must not silently downgrade a real signal to "ignore"."""
        eligible = [t for t in self.tiers if t.sigma <= sigma]
        return max(eligible, key=lambda t: t.sigma) if eligible else None


def _require_mapping(node: Any, what: str) -> dict:
    if not isinstance(node, dict):
        raise BandsConfigError(f"{what} must be a mapping, got {type(node).__name__}")
    return node


def _require_int(node: Any, what: str) -> int:
    # bool is an int subclass — reject it explicitly or `sigma: true` parses as 1.
    if not isinstance(node, int) or isinstance(node, bool):
        raise BandsConfigError(f"{what} must be an integer, got {node!r}")
    return node


def _str_list(node: Any, what: str) -> tuple[str, ...]:
    if node is None:
        return ()
    if not isinstance(node, list) or not all(isinstance(v, str) for v in node):
        raise BandsConfigError(f"{what} must be a list of strings, got {node!r}")
    return tuple(node)


def _parse_tier(name: str, raw: Any) -> BandTier:
    node = _require_mapping(raw, f"tier {name!r}")
    sigma = _require_int(node.get("sigma"), f"tier {name!r}: sigma")
    if sigma < 1:
        raise BandsConfigError(f"tier {name!r}: sigma must be >= 1, got {sigma}")
    response = node.get("response")
    if response not in RESPONSES:
        raise BandsConfigError(
            f"tier {name!r}: response must be one of {', '.join(RESPONSES)}, "
            f"got {response!r}")
    mode = node.get("permission_mode", "full-auto")
    if mode not in VALID_SESSION_MODES:
        raise BandsConfigError(
            f"tier {name!r}: permission_mode {mode!r} is not a valid session mode")
    if response in _SPAWNING_RESPONSES and mode == "interactive":
        raise BandsConfigError(
            f"tier {name!r}: a {response!r} tier spawns an UNATTENDED session, which "
            "would block forever on interactive permission prompts (#750); choose a "
            "non-interactive mode")
    tools = _str_list(node.get("tools"), f"tier {name!r}: tools")
    if response == "log" and tools:
        raise BandsConfigError(
            f"tier {name!r}: a 'log' tier spawns no session, so its tools list must "
            "be empty — a non-empty one would document a constraint nothing applies")
    return BandTier(name=name, sigma=sigma, response=response, tools=tools,
                    permission_mode=mode)


def _parse_metric(raw: Any) -> MetricBand:
    node = _require_mapping(raw, "metric")
    metric_id = node.get("id")
    if not isinstance(metric_id, str) or not metric_id.strip():
        raise BandsConfigError(f"metric: id must be a non-empty string, got {metric_id!r}")
    source = node.get("source")
    if source not in METRIC_SOURCES:
        raise BandsConfigError(
            f"metric {metric_id!r}: source must be one of {', '.join(METRIC_SOURCES)}, "
            f"got {source!r}")
    repo = node.get("repo")
    if not isinstance(repo, str) or "/" not in repo:
        raise BandsConfigError(
            f"metric {metric_id!r}: repo must be an 'owner/name' string, got {repo!r}")
    direction = node.get("direction", "higher_is_worse")
    if direction not in DIRECTIONS:
        raise BandsConfigError(
            f"metric {metric_id!r}: direction must be one of {', '.join(DIRECTIONS)}, "
            f"got {direction!r}")
    baseline_points = _require_int(
        node.get("baseline_points", 30), f"metric {metric_id!r}: baseline_points")
    min_baseline = _require_int(
        node.get("min_baseline", 10), f"metric {metric_id!r}: min_baseline")
    if min_baseline < 2:
        raise BandsConfigError(
            f"metric {metric_id!r}: min_baseline must be >= 2 — a standard deviation "
            f"over fewer than two points is not a baseline, got {min_baseline}")
    if baseline_points < min_baseline:
        raise BandsConfigError(
            f"metric {metric_id!r}: baseline_points ({baseline_points}) is below "
            f"min_baseline ({min_baseline}), so the band could never fire")
    enabled = node.get("enabled", True)
    if not isinstance(enabled, bool):
        raise BandsConfigError(f"metric {metric_id!r}: enabled must be a boolean")
    return MetricBand(
        id=metric_id, source=source, repo=repo,
        title=str(node.get("title") or ""), unit=str(node.get("unit") or ""),
        direction=direction, baseline_points=baseline_points,
        min_baseline=min_baseline, enabled=enabled)


def load_bands_text(text: str) -> BandsConfig:
    """Parse + validate *text* as a bands policy. Raises ``BandsConfigError``.

    Every violation is an error rather than a skipped entry. A monitor that
    quietly dropped an unparseable band would present as "nothing ever breached",
    which is indistinguishable from a healthy system — the one failure mode a
    detection loop must never have.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BandsConfigError(f"bands.yaml is not valid YAML: {exc}") from exc
    doc = _require_mapping(doc or {}, "bands.yaml")

    version = _require_int(doc.get("version"), "version")
    if version != 1:
        raise BandsConfigError(f"unsupported bands.yaml version {version} (expected 1)")

    tiers_node = _require_mapping(doc.get("tiers"), "tiers")
    if not tiers_node:
        raise BandsConfigError("tiers must declare at least one response tier")
    tiers = tuple(sorted(
        (_parse_tier(name, raw) for name, raw in tiers_node.items()),
        key=lambda t: t.sigma))
    sigmas = [t.sigma for t in tiers]
    if len(set(sigmas)) != len(sigmas):
        raise BandsConfigError(
            f"two tiers share a sigma ({sigmas}); the σ→tier mapping must be "
            "unambiguous")

    metrics_node = doc.get("metrics") or []
    if not isinstance(metrics_node, list):
        raise BandsConfigError("metrics must be a list")
    metrics = tuple(_parse_metric(raw) for raw in metrics_node)
    ids = [m.id for m in metrics]
    if len(set(ids)) != len(ids):
        raise BandsConfigError(f"duplicate metric id(s) in {ids}")

    return BandsConfig(version=version, tiers=tiers, metrics=metrics)


def load_bands(path: Path | str | None = None) -> BandsConfig:
    """Load the committed policy (or the file at *path*)."""
    target = Path(path) if path is not None else DEFAULT_BANDS_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise BandsConfigError(f"cannot read bands config at {target}: {exc}") from exc
    return load_bands_text(text)
