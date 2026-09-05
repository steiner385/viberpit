"""THE quota contract: ``QuotaSnapshot`` / ``QuotaWindow``.

One provider-neutral DTO for "how much of this account's allowance is spent".
It replaces the Anthropic-shaped ``oauth.Usage`` dataclass outright — there is no
adapter and no second usage type, because two concurrent usage contracts is the
parallel-implementation shape the constitution's single-source-of-truth principle
exists to stop.

Three rules the shape enforces, each of which was a real defect in the two-field
predecessor:

* **Windows are a list, not two named dimensions.** ``Usage`` hard-coded
  ``session_pct`` + ``weekly_pct``, so a provider that meters ONE allowance had to
  invent a second. ``copilot.usage_from_proxy`` did exactly that — it projected the
  premium-request percentage onto both fields, and the UI rendered the same number
  twice under two different labels. A provider now reports the windows it actually
  has.
* **Never fabricate a window.** An unknown or failed read is expressed as
  ``availability``, not as a zero-percent window (which reads as "plenty of quota")
  and not as a 100% one (which reads as "exhausted"). ``AVAILABLE`` is the only
  availability whose windows carry meaning.
* **The binding window is computed, never assumed.** Callers ask
  :meth:`QuotaSnapshot.binding_window` which allowance runs out first for a given
  model; they do not reach for a field named "weekly".

Timestamps are timezone-aware UTC throughout (``resets_at``, ``fetched_at``).
Conversion to ``America/New_York`` happens only at a presentation boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

# Bumped whenever the persisted (ledger) encoding of a snapshot changes shape.
# A blob with no version key predates versioning and is the legacy two-field
# ``Usage`` encoding — see ``ledger._usage_from_blob``.
QUOTA_BLOB_VERSION = 2

# --- availability -----------------------------------------------------------
# How much the snapshot's windows can be trusted. Anything other than AVAILABLE
# means "we do not know this account's headroom"; the selector treats those
# exactly as it treated a ``None`` usage before (conservatively excluded unless
# the caller opted into optimistic fallback).
AVAILABLE = "available"
UNKNOWN = "unknown"          # transient/unspecified read failure
AUTH_ERROR = "auth_error"    # credentials rejected — needs re-auth, not a retry
UNSUPPORTED = "unsupported"  # provider boundary does not expose quota

AVAILABILITIES = frozenset({AVAILABLE, UNKNOWN, AUTH_ERROR, UNSUPPORTED})

# --- window scope -----------------------------------------------------------
# ACCOUNT: the window meters everything the account does.
# MODEL:   the window meters only the models in ``applies_to_models``.
SCOPE_ACCOUNT = "account"
SCOPE_MODEL = "model"

# --- well-known window keys -------------------------------------------------
# Keys are a wire contract (ledger blobs, the accounts API, the browser); never
# rename one. New providers add new keys rather than reusing these.
WINDOW_SESSION = "session"   # Anthropic rolling 5-hour window
WINDOW_WEEKLY = "weekly"     # Anthropic rolling 7-day window
WINDOW_PREMIUM = "premium"   # Copilot premium-request allowance

_FIVE_HOURS_S = 5 * 3600
_SEVEN_DAYS_S = 7 * 24 * 3600


def _parse_dt(s: Any) -> datetime | None:
    """Parse a persisted ISO timestamp back to an aware UTC datetime."""
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(s))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


@dataclass(frozen=True)
class QuotaWindow:
    """One metered allowance on one account.

    ``used_pct`` is 0–100. ``resets_at`` is None when the provider reports no
    ACTIVE window (a fully fresh account has nothing to reset) — that is a real
    state, not an error, and the selector reads it as "this capacity is not
    perishable yet".
    """

    key: str
    label: str
    used_pct: float
    resets_at: datetime | None = None
    duration_seconds: int | None = None
    scope: str = SCOPE_ACCOUNT
    applies_to_models: tuple[str, ...] = ()

    def applies_to(self, model: str | None) -> bool:
        """True when this window meters *model*.

        An empty ``applies_to_models`` means "every model". An UNKNOWN model
        (None) matches every window on purpose: the caller has not told us what
        it will run, so assuming a model-scoped limit does not apply would hand
        out an account that 429s on its first turn.
        """
        if self.scope != SCOPE_MODEL or not self.applies_to_models:
            return True
        if model is None:
            return True
        return model in self.applies_to_models

    def remaining_pct(self, ceiling: float) -> float:
        return ceiling - self.used_pct

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "used_pct": self.used_pct,
            "resets_at": _iso(self.resets_at),
            "duration_seconds": self.duration_seconds,
            "scope": self.scope,
            "applies_to_models": list(self.applies_to_models),
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "QuotaWindow":
        return cls(
            key=str(d["key"]),
            label=str(d.get("label") or d["key"]),
            used_pct=float(d["used_pct"]),
            resets_at=_parse_dt(d.get("resets_at")),
            duration_seconds=(int(d["duration_seconds"])
                              if d.get("duration_seconds") is not None else None),
            scope=str(d.get("scope") or SCOPE_ACCOUNT),
            applies_to_models=tuple(d.get("applies_to_models") or ()),
        )


@dataclass(frozen=True)
class QuotaSnapshot:
    """Every window a provider reported for one account at one instant."""

    provider: str
    fetched_at: datetime
    availability: str = AVAILABLE
    windows: tuple[QuotaWindow, ...] = ()
    # Whether the PROVIDER has paid overflow ("extra usage") switched on. Not a
    # window — it is a boolean capability of the account, and the selector's paid
    # tier requires it in addition to the local opt-in. Kept on the snapshot
    # because it arrives on the same upstream read as the windows do.
    extra_usage_enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "windows", tuple(self.windows))
        object.__setattr__(self, "fetched_at", _parse_dt(self.fetched_at))
        if self.availability not in AVAILABILITIES:
            raise ValueError(f"unknown quota availability {self.availability!r}")

    # -- queries ------------------------------------------------------------

    @property
    def is_available(self) -> bool:
        return self.availability == AVAILABLE

    def windows_for(self, model: str | None = None) -> tuple[QuotaWindow, ...]:
        """The windows that meter *model* (all of them when model is None)."""
        return tuple(w for w in self.windows if w.applies_to(model))

    def window(self, key: str) -> QuotaWindow | None:
        for w in self.windows:
            if w.key == key:
                return w
        return None

    def binding_window(self, model: str | None = None) -> QuotaWindow | None:
        """The window that runs out FIRST for *model* — the most-used applicable one.

        This is the single definition of "which allowance binds this account",
        replacing the old ``if weekly_remaining < session_remaining`` pair math.
        Ties resolve to the EARLIER window in provider order (``max`` returns the
        first maximal element), which reproduces the predecessor exactly: it only
        switched to weekly when weekly was STRICTLY more used than session.
        Returns None when the snapshot has no applicable window.
        """
        applicable = self.windows_for(model)
        if not applicable:
            return None
        return max(applicable, key=lambda w: w.used_pct)

    def max_used_pct(self, model: str | None = None) -> float | None:
        w = self.binding_window(model)
        return None if w is None else w.used_pct

    def windows_at_or_over(self, pct: float,
                           model: str | None = None) -> tuple[QuotaWindow, ...]:
        return tuple(w for w in self.windows_for(model) if w.used_pct >= pct)

    def reserve_window(self) -> QuotaWindow | None:
        """The window a per-account usage RESERVE applies to.

        The reserve is a "don't burn the long-horizon allowance" control, so it
        binds the LONGEST window the provider reports: Anthropic's weekly (which
        is what the setting has always meant, and is still named after), and —
        for a provider that meters a single allowance, like Copilot — that single
        window. Returns None only when the provider reports several windows and
        none of them declares a duration, which no current provider does.
        """
        if not self.windows:
            return None
        if len(self.windows) == 1:
            return self.windows[0]
        timed = [w for w in self.windows if w.duration_seconds is not None]
        if not timed:
            return None
        return max(timed, key=lambda w: w.duration_seconds or 0)

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> dict:
        """The persisted / RPC encoding. Versioned so a reader can migrate."""
        return {
            "v": QUOTA_BLOB_VERSION,
            "provider": self.provider,
            "fetched_at": _iso(self.fetched_at),
            "availability": self.availability,
            "extra_usage_enabled": self.extra_usage_enabled,
            "windows": [w.to_dict() for w in self.windows],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "QuotaSnapshot":
        # A payload with NO ``windows`` key carries no headroom information at
        # all, so it decodes as UNKNOWN. Defaulting it to AVAILABLE would make
        # this module fabricate exactly the thing its second rule forbids
        # fabricating: an "available, zero windows" snapshot that the selector
        # would have to guess about. An EXPLICIT ``availability`` always wins,
        # and an explicit (even empty) ``windows`` list is a real answer.
        raw_windows = d.get("windows")
        default_availability = AVAILABLE if raw_windows is not None else UNKNOWN
        return cls(
            provider=str(d.get("provider") or "anthropic"),
            fetched_at=_parse_dt(d.get("fetched_at")) or datetime.now(timezone.utc),
            availability=str(d.get("availability") or default_availability),
            windows=tuple(QuotaWindow.from_dict(w) for w in (raw_windows or ())),
            extra_usage_enabled=bool(d.get("extra_usage_enabled")),
        )


# ---------------------------------------------------------------------------
# Provider constructors
#
# The ONE place each provider's window layout is spelled out, so a producer
# (a live fetch) and a reader (the ledger's legacy migration) cannot disagree
# about how many windows that provider has or what they are called.
# ---------------------------------------------------------------------------


def anthropic_snapshot(session_pct: float,
                       session_resets_at: datetime | None,
                       weekly_pct: float,
                       weekly_resets_at: datetime | None,
                       extra_usage_enabled: bool = False,
                       *,
                       fetched_at: datetime | None = None,
                       availability: str = AVAILABLE) -> QuotaSnapshot:
    """Anthropic reports TWO windows: a rolling 5-hour one and a rolling 7-day one."""
    return QuotaSnapshot(
        provider="anthropic",
        fetched_at=fetched_at or datetime.now(timezone.utc),
        availability=availability,
        windows=(
            QuotaWindow(key=WINDOW_SESSION, label="session", used_pct=float(session_pct),
                        resets_at=session_resets_at, duration_seconds=_FIVE_HOURS_S),
            QuotaWindow(key=WINDOW_WEEKLY, label="weekly", used_pct=float(weekly_pct),
                        resets_at=weekly_resets_at, duration_seconds=_SEVEN_DAYS_S),
        ),
        extra_usage_enabled=bool(extra_usage_enabled),
    )


def copilot_snapshot(premium_pct: float,
                     resets_at: datetime | None = None,
                     *,
                     fetched_at: datetime | None = None,
                     availability: str = AVAILABLE) -> QuotaSnapshot:
    """Copilot meters ONE premium-request allowance — exactly one window.

    The predecessor projected this single number onto both ``session_pct`` and
    ``weekly_pct``; the duplicate was rendered twice in the UI and made the
    selector's "which dimension binds" question meaningless for Copilot.
    """
    return QuotaSnapshot(
        provider="copilot",
        fetched_at=fetched_at or datetime.now(timezone.utc),
        availability=availability,
        windows=(
            QuotaWindow(key=WINDOW_PREMIUM, label="premium-req",
                        used_pct=float(premium_pct), resets_at=resets_at),
        ),
        extra_usage_enabled=False,
    )


def unknown_snapshot(provider: str, *, fetched_at: datetime | None = None,
                     availability: str = UNKNOWN) -> QuotaSnapshot:
    """A read that failed. No windows — absence of data, never a fabricated 0%."""
    return QuotaSnapshot(
        provider=provider,
        fetched_at=fetched_at or datetime.now(timezone.utc),
        availability=availability,
        windows=(),
    )
