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


class Histogram:
    """Tracks count + sum of observed values (no bucket support — not needed by
    any current consumer; add buckets here if one shows up rather than a second
    histogram type)."""

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help_text = help_text
        self._lock = threading.Lock()
        self._count = 0
        self._sum = 0.0

    def observe(self, value: float) -> None:
        with self._lock:
            self._count += 1
            self._sum += value

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def sum(self) -> float:
        with self._lock:
            return self._sum


class Gauge:
    """A value that can go up or down."""

    def __init__(self, name: str, help_text: str) -> None:
        self.name = name
        self.help_text = help_text
        self._lock = threading.Lock()
        self._value = 0.0

    def inc(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value += amount

    def dec(self, amount: float = 1.0) -> None:
        with self._lock:
            self._value -= amount

    @property
    def value(self) -> float:
        with self._lock:
            return self._value


# Assistant metrics (spec §4.10). Instantiated here, at module level, so every
# call site imports the SAME instance — the one source of truth for these five
# series.
assistant_prompts_total = Counter("assistant_prompts_total", "Assistant prompts submitted")
assistant_tool_calls_total = Counter(
    "assistant_tool_calls_total", "Assistant tool calls", labelnames=("tool", "class", "outcome"))
assistant_ttft_ms = Histogram("assistant_ttft_ms", "Assistant time-to-first-token (ms)")
assistant_turn_ms = Histogram("assistant_turn_ms", "Assistant turn duration (ms)")
assistant_sessions_live = Gauge("assistant_sessions_live", "Live assistant sessions")
