"""Minimal in-process, thread-safe metrics primitives — no external dependency.

``server/obs/`` is dependency-free by convention (``jsonlog.py``, ``loop_lag.py``,
``tasks.py`` are all hand-rolled), and this repo has no ``prometheus_client``
dependency and no existing counter/histogram registry anywhere in ``server/`` —
verified by grep before writing this. Do not add ``prometheus_client``; this module
IS the registry other assistant-launch code (spec §4.10) wires into.

No HTTP scrape endpoint is provided (out of scope for the launch task) — these are
read directly by tests and, if a scrape surface is ever added, by that endpoint.
"""
from __future__ import annotations

import threading

from ccswitch import brand

LabelValues = tuple[str, ...]


class _CounterChild:
    """A `Counter` bound to one label-value tuple."""

    def __init__(self, lock: threading.Lock, store: dict[LabelValues, float], key: LabelValues) -> None:
        self._lock = lock
        self._store = store
        self._key = key

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._store[self._key] = self._store.get(self._key, 0.0) + amount


class Counter:
    """A monotonic counter, optionally labelled.

    Label VALUES are always positional (never keyword) — one of this project's
    label names is literally ``class``, a Python reserved word, so a keyword-arg
    API would be unusable for it.
    """

    def __init__(self, name: str, help_text: str, labelnames: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.labelnames = labelnames
        self._lock = threading.Lock()
        self._values: dict[LabelValues, float] = {}

    def inc(self, amount: float = 1.0) -> None:
        if self.labelnames:
            raise TypeError(f"Counter {self.name!r} requires labels {self.labelnames!r}; use .labels(...)")
        with self._lock:
            self._values[()] = self._values.get((), 0.0) + amount

    def labels(self, *values: str) -> _CounterChild:
        if len(values) != len(self.labelnames):
            raise TypeError(
                f"Counter {self.name!r} expects {len(self.labelnames)} label value(s) "
                f"{self.labelnames!r}, got {len(values)}"
            )
        return _CounterChild(self._lock, self._values, tuple(values))

    def value(self, *label_values: str) -> float:
        """Test reader: the current value for a label combination (or the bare
        counter when label-less)."""
        with self._lock:
            return self._values.get(tuple(label_values), 0.0)


class _HistogramChild:
    """A `Histogram` bound to one label-value tuple."""

    def __init__(
        self,
        lock: threading.Lock,
        counts: dict[LabelValues, int],
        sums: dict[LabelValues, float],
        key: LabelValues,
    ) -> None:
        self._lock = lock
        self._counts = counts
        self._sums = sums
        self._key = key

    def observe(self, value: float) -> None:
        with self._lock:
            self._counts[self._key] = self._counts.get(self._key, 0) + 1
            self._sums[self._key] = self._sums.get(self._key, 0.0) + value


class Histogram:
    """Tracks count + sum of observed values, optionally labelled (no bucket
    support — not needed by any current consumer; add buckets here if one shows up
    rather than a second histogram type)."""

    def __init__(self, name: str, help_text: str, labelnames: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.labelnames = labelnames
        self._lock = threading.Lock()
        self._counts: dict[LabelValues, int] = {}
        self._sums: dict[LabelValues, float] = {}

    def observe(self, value: float) -> None:
        if self.labelnames:
            raise TypeError(
                f"Histogram {self.name!r} requires labels {self.labelnames!r}; use .labels(...)"
            )
        with self._lock:
            self._counts[()] = self._counts.get((), 0) + 1
            self._sums[()] = self._sums.get((), 0.0) + value

    def labels(self, *values: str) -> _HistogramChild:
        if len(values) != len(self.labelnames):
            raise TypeError(
                f"Histogram {self.name!r} expects {len(self.labelnames)} label value(s) "
                f"{self.labelnames!r}, got {len(values)}"
            )
        return _HistogramChild(self._lock, self._counts, self._sums, tuple(values))

    @property
    def count(self) -> int:
        return self.count_for()

    @property
    def sum(self) -> float:
        return self.sum_for()

    def count_for(self, *label_values: str) -> int:
        """Test reader: observation count for a label combination."""
        with self._lock:
            return self._counts.get(tuple(label_values), 0)

    def sum_for(self, *label_values: str) -> float:
        """Test reader: observation sum for a label combination."""
        with self._lock:
            return self._sums.get(tuple(label_values), 0.0)


class _GaugeChild:
    """A `Gauge` bound to one label-value tuple."""

    def __init__(
        self, lock: threading.Lock, store: dict[LabelValues, float], key: LabelValues
    ) -> None:
        self._lock = lock
        self._store = store
        self._key = key

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._store[self._key] = self._store.get(self._key, 0.0) + amount

    def dec(self, amount: float = 1.0) -> None:
        self.inc(-amount)

    def set(self, value: float) -> None:
        with self._lock:
            self._store[self._key] = value


class Gauge:
    """A value that can go up or down, optionally labelled."""

    def __init__(self, name: str, help_text: str, labelnames: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.labelnames = labelnames
        self._lock = threading.Lock()
        self._values: dict[LabelValues, float] = {}

    def _require_unlabelled(self) -> None:
        if self.labelnames:
            raise TypeError(
                f"Gauge {self.name!r} requires labels {self.labelnames!r}; use .labels(...)"
            )

    def inc(self, amount: float = 1.0) -> None:
        self._require_unlabelled()
        with self._lock:
            self._values[()] = self._values.get((), 0.0) + amount

    def dec(self, amount: float = 1.0) -> None:
        self.inc(-amount)

    def set(self, value: float) -> None:
        self._require_unlabelled()
        with self._lock:
            self._values[()] = value

    def labels(self, *values: str) -> _GaugeChild:
        if len(values) != len(self.labelnames):
            raise TypeError(
                f"Gauge {self.name!r} expects {len(self.labelnames)} label value(s) "
                f"{self.labelnames!r}, got {len(values)}"
            )
        return _GaugeChild(self._lock, self._values, tuple(values))

    @property
    def value(self) -> float:
        return self.value_for()

    def value_for(self, *label_values: str) -> float:
        """Test reader: the current value for a label combination."""
        with self._lock:
            return self._values.get(tuple(label_values), 0.0)


# Assistant metrics (spec §4.10). Instantiated here, at module level, so every
# call site imports the SAME instance — the one source of truth for these six
# series.
assistant_prompts_total = Counter("assistant_prompts_total", "Assistant prompts submitted")
assistant_tool_calls_total = Counter(
    "assistant_tool_calls_total", "Assistant tool calls", labelnames=("tool", "class", "outcome"))
assistant_ttft_ms = Histogram("assistant_ttft_ms", "Assistant time-to-first-token (ms)")
assistant_turn_ms = Histogram("assistant_turn_ms", "Assistant turn duration (ms)")
assistant_sessions_live = Gauge("assistant_sessions_live", "Live assistant sessions")
# Counts the times the assistant told a user "no tool can do that" (spec v2 §4.0,
# gap G15). The `capability` label is a short kebab-case slug the model supplies,
# so the product team can rank what the drawer is asked for but cannot do.
assistant_gap_reports_total = Counter(
    "assistant_gap_reports_total", "Capability gaps the assistant reported",
    labelnames=("capability",))

# Session tool surface (session tools spec §3, §4). Same shape as the assistant's
# counters so one Grafana row can compare the two surfaces.
session_tool_calls_total = Counter(
    "session_tool_calls_total", "Coding-session ccswitch tool calls",
    labelnames=("tool", "class", "outcome"))
session_feedback_total = Counter(
    "session_feedback_total", "Feedback reports coding sessions made about ccswitch",
    labelnames=("category",))

# Codex boundary metrics (plan Task 6.1: "engine/provider/host/version/method
# latency, active threads/turns, restarts, auth generation/health, quota age, and
# approval latency").
#
# What is deliberately NOT collected: native Codex thread ids, turn ids, prompts,
# commands, patches, and model output. The plan says "hash native ids"; the label
# sets below go further and never collect one, because a hashed thread id is still
# unbounded cardinality and no series here needs per-thread resolution. `account`
# is a ccswitch alias — public in the account manifest and already in the audit
# trail — and every other label is a closed vocabulary (a supported method name, a
# fixed outcome, a typed reason).
#
# `host` and `cli_version` are PROCESS constants: one daemon serves one host and
# runs one Codex binary. They therefore live on an info gauge that the other series
# join against, rather than being repeated on every observation — the standard
# shape for target-scoped labels, and the alternative would have meant threading
# two more constructor arguments through the client-factory seam that every test
# fake reimplements.
codex_build_info = Gauge(
    "codex_build_info",
    "1 per live Codex boundary, labelled with the identity the other series join on",
    labelnames=("engine", "provider", "host", "cli_version"),
)
codex_request_ms = Histogram(
    "codex_request_ms",
    "Codex app-server request round-trip (ms)",
    labelnames=("method", "outcome"),
)
codex_threads_active = Gauge(
    "codex_threads_active",
    f"Codex threads registered to a live {brand.NAME} session",
    labelnames=("account",),
)
codex_turns_active = Gauge(
    "codex_turns_active", "Codex turns currently executing", labelnames=("account",)
)
codex_runner_restarts_total = Counter(
    "codex_runner_restarts_total",
    "Codex app-server supervision restarts",
    labelnames=("account", "outcome"),
)
codex_auth_requests_total = Counter(
    "codex_auth_requests_total",
    "Credential-authority calls by method and outcome",
    labelnames=("method", "outcome"),
)
codex_auth_request_ms = Histogram(
    "codex_auth_request_ms",
    "Credential-authority call latency (ms)",
    labelnames=("method", "outcome"),
)
codex_auth_generation = Gauge(
    "codex_auth_generation",
    "Newest credential generation this process has seen for an account",
    labelnames=("account",),
)
# An "age" cannot be a gauge that something has to keep re-setting — it would be
# stale by construction between updates. This carries the READ TIME instead, and a
# consumer computes `now - value`, which is the standard way to express an age.
codex_quota_last_read_timestamp_s = Gauge(
    "codex_quota_last_read_timestamp_s",
    "Unix time of the newest good Codex quota read (age = now - value)",
    labelnames=("account",),
)
codex_approval_ms = Histogram(
    "codex_approval_ms",
    "Time from a Codex approval request arriving to its decision (ms)",
    labelnames=("outcome",),
)
codex_user_input_ms = Histogram(
    "codex_user_input_ms",
    "Time from a Codex request_user_input request arriving to its answer (ms)",
    labelnames=("outcome",),
)
codex_dynamic_tool_ms = Histogram(
    "codex_dynamic_tool_ms",
    "Time from a Codex item/tool/call arriving to its result (ms)",
    labelnames=("outcome",),
)
