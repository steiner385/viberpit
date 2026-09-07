"""Control-Band Monitor — the back-of-loop automation that closes the SDLC cycle.

Phase 2 of the AI-Native SDLC design (§3.2/§4/§9, issue #804). The playbook's rule
is *detection deterministic, judgment gated*, and this module is where the two
meet: ``server.sdlc.control_bands`` decides — with statistics and no model — that
something moved, ``server/sdlc/bands.yaml`` decides what ccswitch is allowed to do
about it, and only then may a session be spawned.

Two objects, the same mold as ``BacklogRunner`` / ``SdlcIntake`` (constitution VI —
one substrate, no parallel engine):

* ``ControlBandMonitor`` — the ``Automation`` contract surface. Metadata only.
* ``ControlBandMonitorLoop`` — the supervised control loop. Every I/O dependency is
  INJECTED (read config, read the bands policy, read a metric series, file or
  dispose a triage item, spawn a diagnosis session, clock), so the loop runs with
  no network and no real DB in tests.

How the loop closes (design §4 step 7 → step 1)
-----------------------------------------------
A 3σ breach does **not** write ``intent.md`` here. It queues a triage item, and
the existing ``SdlcIntakeLoop`` — the one and only path from a trigger to a
committed ``intent.md`` PR — picks queued items up on its own tick. Phase 2 feeds
the Phase 1 intake seam rather than forking a second intent writer; the seam was
left deliberately empty for exactly this (``_sdlc_intake_read_triggers``).

Response by tier (bands.yaml):

===========  ==============================================================
1σ ``log``   File the item. No session. Most 1σ excursions are variation.
2σ           File it and spawn ONE read-only diagnosis session, linked to the
``diagnose`` item. It reports; it changes nothing.
3σ           File it and queue it for SDLC Intake → ``intent.md`` PR →
``propose``  Initiative. The loop is closed.
===========  ==============================================================
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from server.db.models import TRIAGE_TERMINAL_STATUSES
from server.sdlc.bands import BandsConfig, BandTier, MetricBand
from server.sdlc.control_bands import Breach, Sample, evaluate
from server.sdlc.prompts import build_diagnosis_prompt
from server.sdlc.slug import slugify

from .base import AutomationStatus, ConfigField
from .engine_policy import EnginePolicy

log = logging.getLogger(__name__)

# Diagnosis spawns are capped over a rolling 24h window (mirrors SdlcIntakeLoop).
_DAY_SECONDS = 24 * 60 * 60


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O — unit-tested directly)                                  #
# --------------------------------------------------------------------------- #

def incident_slug(breach: Breach) -> str:
    """The Initiative slug a 3σ incident would adopt.

    Derived deterministically from the breach identity and the observation date,
    so a tick that re-queues the same ongoing incident names the same initiative
    instead of spawning a second one. The date is part of it on purpose: a breach
    of the same metric a month later is a new incident, not a resumption.
    """
    return slugify(f"incident {breach.metric} {breach.rule} {breach.side} {breach.at}")


def next_disposition(current: str | None, response: str) -> str | None:
    """The status a freshly-detected breach should move its item to, or ``None``
    to leave the row's disposition untouched. Pure + total.

    The rule that matters: **an already-dispositioned item is never re-driven.**
    A control-band breach commonly persists for days, so the monitor sees it on
    every tick. Without this, an item a human dismissed would re-open, an item
    already queued would be queued again, and a diagnosis session would be
    respawned every minute for as long as the metric stayed out of band.
    """
    if current in TRIAGE_TERMINAL_STATUSES:
        return None
    if current in ("diagnosing", "queued", "scheduled"):
        return None
    # current is None (a brand-new row) or "open".
    if response == "diagnose":
        return "diagnosing"
    if response == "propose":
        return "queued"
    return None  # "log" — the item rests at open; filing it IS the response.


# --------------------------------------------------------------------------- #
# Loop I/O views                                                                #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FiledItem:
    """What the ``file_item`` seam returns after upserting a breach.

    ``created`` distinguishes a first sighting from a re-observation, which is
    what decides whether the loop emits a new-incident event or quietly refreshes
    the measurements on a row that is already someone's problem."""
    id: str
    key: str
    status: str
    created: bool


# --------------------------------------------------------------------------- #
# Automation contract surface                                                   #
# --------------------------------------------------------------------------- #

class ControlBandMonitor:
    id = "control-band-monitor"
    name = "Control-Band Monitor"
    description = (
        "Watches ccswitch's own delivery metrics against version-controlled "
        "control bands. A deterministic breach files a triage item and, by tier, "
        "spawns a read-only diagnosis (2σ) or queues an intent.md (3σ).")
    #: ``monitor`` detects and files ONLY — no session, no queueing, whatever the
    #: tier says. It is the safe way to turn this on: you get the triage queue and
    #: can see what the bands would have done before letting them do it.
    #: ``propose`` enables the full tier ladder.
    modes: tuple[str, ...] = ("off", "monitor", "propose")
    #: Claude-only. The diagnosis prompt is a repo-investigation task whose whole
    #: value is the conclusion it writes, and no Codex acceptance suite exists for
    #: this origin — an unaudited engine must fail closed (Phase 7, Task 7.1).
    engine_policy: EnginePolicy = EnginePolicy.CLAUDE_ONLY
    config_schema = [
        ConfigField("enabled_repos", "Repositories", "repo-multiselect", [],
                    help="Restrict the bands.yaml metrics to these repositories. "
                         "Empty means every repository the policy names."),
        ConfigField("max_diagnoses_per_day", "Max diagnosis sessions / day", "int", 3,
                    help="Safety cap on 2σ read-only diagnosis sessions per rolling 24h."),
        ConfigField("max_open_incidents", "Max incidents queued for intake", "int", 3,
                    help="Ceiling on triage items simultaneously queued for SDLC "
                         "Intake. A metric storm must not open a dozen intent PRs."),
    ]

    def status(self, ctx: Any) -> AutomationStatus:
        counts = getattr(ctx, "control_band_counts", None) if ctx else None
        if not counts:
            return AutomationStatus(text="idle", level="idle")
        watched = counts.get("watched", 0)
        breaching = counts.get("breaching", 0)
        text = f"{watched} band{'' if watched == 1 else 's'} watched · {breaching} breaching"
        return AutomationStatus(
            text=text, level="warn" if breaching else "idle", counts=counts)

    async def on_state_change(self, *, enabled: bool, mode: str | None,
                              config: dict, ctx: Any) -> None:
        # No side effects on toggle; the loop reacts on its next tick.
        return None


# --------------------------------------------------------------------------- #
# Injected-seam type aliases                                                    #
# --------------------------------------------------------------------------- #

ReadConfig = Callable[[], Awaitable[tuple[str, dict]]]
ReadBands = Callable[[], Awaitable[BandsConfig]]
ReadSeries = Callable[[MetricBand], Awaitable[list[Sample]]]
#: ``(band, breach, tier, slug) -> FiledItem`` — upsert by ``breach.key()``.
FileItem = Callable[[MetricBand, Breach, BandTier, str], Awaitable[FiledItem]]
#: ``(item_id, status, session_id) -> None``.
SetDisposition = Callable[[str, str, str | None], Awaitable[None]]
#: ``(item_id, prompt, band, tier) -> session | None`` (None = spawn declined).
SpawnDiagnosis = Callable[[str, str, MetricBand, BandTier], Awaitable[Any]]
CountQueued = Callable[[], Awaitable[int]]
#: Return scheduled items whose time has come to ``open``; answers how many.
ReleaseDue = Callable[[], Awaitable[int]]


class ControlBandMonitorLoop:
    """The Maintain-loop control loop. One ``tick()`` evaluates every enabled band
    and drives at most one response per breaching metric. All I/O is injected."""

    def __init__(
        self,
        ctx: Any,
        *,
        read_config: ReadConfig,
        read_bands: ReadBands,
        read_series: ReadSeries,
        file_item: FileItem,
        set_disposition: SetDisposition,
        spawn_diagnosis: SpawnDiagnosis,
        count_queued: CountQueued,
        release_due: ReleaseDue,
        clock: Callable[[], float],
    ) -> None:
        self._ctx = ctx
        self._read_config = read_config
        self._read_bands = read_bands
        self._read_series = read_series
        self._file_item = file_item
        self._set_disposition = set_disposition
        self._spawn_diagnosis = spawn_diagnosis
        self._count_queued = count_queued
        self._release_due = release_due
        self._clock = clock
        # Rolling-window diagnosis-spawn timestamps (in-memory, like SdlcIntakeLoop).
        self._diagnoses_today: list[float] = []

    async def tick(self) -> None:
        """One control-loop iteration. Best-effort: swallows exceptions so a
        transient failure never crashes the supervising driver."""
        try:
            await self._run()
        except Exception:  # pragma: no cover — belt+braces, mirrors the mold
            log.warning("control_band_monitor.tick_error", exc_info=True)

    async def _run(self) -> None:
        mode, config = await self._read_config()
        if mode == "off":
            self._stash_counts(0, 0)
            return

        # Deferred incidents come back BEFORE detection runs, so an item whose
        # snooze expired is `open` again by the time this tick decides what to do
        # with the breach that is still producing it. Without this, "schedule for
        # later" would be a black hole — the loop never re-drives a dispositioned
        # item, so a scheduled incident would rest there forever.
        released = await self._release_due()
        if released:
            await self._ctx.emit(
                "info", f"{released} scheduled incident(s) returned to the queue",
                kind="released")

        try:
            bands = await self._read_bands()
        except Exception as exc:
            # A malformed policy is loud, not silent: a monitor that quietly
            # watched nothing would look exactly like a healthy system.
            await self._ctx.emit("error", f"bands policy failed to load: {exc}",
                                 kind="bands_invalid")
            self._stash_counts(0, 0)
            return

        allowed_repos = {r for r in (config.get("enabled_repos") or []) if r}
        metrics = [m for m in bands.enabled_metrics()
                   if not allowed_repos or m.repo in allowed_repos]
        self._prune_day()
        breaching = 0

        for band in metrics:
            breach = await self._evaluate_band(band)
            if breach is None:
                continue
            breaching += 1
            tier = bands.tier_for_sigma(breach.sigma)
            if tier is None:
                await self._ctx.emit(
                    "warn",
                    f"{band.id}: {breach.sigma}σ breach with no tier at or below it "
                    "in bands.yaml; not responding",
                    kind="no_tier", target=band.id)
                continue
            await self._respond(band, breach, tier, mode, config)

        self._stash_counts(len(metrics), breaching)

    async def _evaluate_band(self, band: MetricBand) -> Breach | None:
        """Read one band's series and run the deterministic detector over it.

        A series read that fails is a warning, not a tick failure: one unreadable
        metric must not stop the others from being watched."""
        try:
            samples = await self._read_series(band)
        except Exception:
            log.warning("control_band_monitor.series_error metric=%s", band.id,
                        exc_info=True)
            await self._ctx.emit("warn", f"could not read series for {band.id}",
                                 kind="series_error", target=band.id)
            return None
        return evaluate(
            band.id, samples,
            direction=band.direction,
            baseline_points=band.baseline_points,
            min_baseline=band.min_baseline,
        )

    async def _respond(self, band: MetricBand, breach: Breach, tier: BandTier,
                       mode: str, config: dict) -> None:
        """File the incident, then take the tier's response — once."""
        slug = incident_slug(breach)
        item = await self._file_item(band, breach, tier, slug)
        if item.created:
            await self._ctx.emit(
                "warn", breach.summary(), kind="breach", target=band.id,
                detail={"itemId": item.id, "key": item.key, "tier": tier.name,
                        "rule": breach.rule, "sigma": breach.sigma})

        # `monitor` mode is detection-only: the queue fills and the feed reports,
        # but no session is spawned and nothing is handed to Intake. This is what
        # makes turning the automation on a reversible, observable decision.
        if mode != "propose":
            return

        target = next_disposition(item.status, tier.response)
        if target is None:
            return

        if target == "diagnosing":
            await self._diagnose(band, breach, tier, item, config)
        elif target == "queued":
            await self._queue_for_intake(band, breach, item, slug, config)

    async def _diagnose(self, band: MetricBand, breach: Breach, tier: BandTier,
                        item: FiledItem, config: dict) -> None:
        cap = int(config.get("max_diagnoses_per_day", 3))
        if len(self._diagnoses_today) >= cap:
            await self._ctx.emit(
                "info", f"daily diagnosis cap reached; {band.id} left open",
                kind="ceiling", target=band.id)
            return
        prompt = build_diagnosis_prompt(
            breach, tools=tier.tools, repo=band.repo, unit=band.unit)
        session = await self._spawn_diagnosis(item.id, prompt, band, tier)
        if session is None:
            # The spawn declined (engine policy, an engine/permission-mode pair
            # that could not do the job, or no account with headroom). Leave the
            # item OPEN: marking it "diagnosing" would park the incident behind a
            # session that does not exist, and the spawn helper has already
            # recorded why. A later tick retries.
            return
        session_id = getattr(session, "id", None)
        await self._set_disposition(item.id, "diagnosing", session_id)
        self._diagnoses_today.append(self._clock())
        await self._ctx.emit(
            "info", f"diagnosing {band.id} ({breach.sigma}σ, read-only)",
            kind="diagnosing", target=band.id, session_id=session_id,
            detail={"itemId": item.id})

    async def _queue_for_intake(self, band: MetricBand, breach: Breach,
                                item: FiledItem, slug: str, config: dict) -> None:
        cap = int(config.get("max_open_incidents", 3))
        queued = await self._count_queued()
        if queued >= cap:
            await self._ctx.emit(
                "info",
                f"{queued} incident(s) already queued for intake (cap {cap}); "
                f"{band.id} left open",
                kind="ceiling", target=band.id)
            return
        await self._set_disposition(item.id, "queued", None)
        await self._ctx.emit(
            "warn",
            f"queued {band.id} for SDLC Intake as initiative '{slug}' "
            f"({breach.sigma}σ)",
            kind="queued", target=band.id,
            detail={"itemId": item.id, "slug": slug})

    def _prune_day(self) -> None:
        """Drop diagnosis-spawn timestamps older than the rolling 24h window."""
        cutoff = self._clock() - _DAY_SECONDS
        self._diagnoses_today = [t for t in self._diagnoses_today if t >= cutoff]

    def _stash_counts(self, watched: int, breaching: int) -> None:
        # Surfaced by ControlBandMonitor.status() on the automation card.
        self._ctx.control_band_counts = {"watched": watched, "breaching": breaching}
