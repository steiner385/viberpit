"""The one fenced-JSON extractor for session output (constitution VI).

Every "ask a session for structured output" parser in `server/pm/` reads the
same way: prefer a ```json fenced block, fall back to the outermost bracket
pair in the prose, and fail loudly when neither yields JSON of the expected
shape. Classification wants an array, materialization an object; the tolerance
is identical, so there is exactly one implementation of it here rather than a
regex per parser. Callers wrap `FencedJsonError` in their own domain error.

Pure: no I/O, no model calls.
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

Shape = Literal["array", "object"]

_BRACKETS: dict[str, tuple[str, str]] = {"array": ("[", "]"), "object": ("{", "}")}
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class FencedJsonError(ValueError):
    """No JSON of the requested shape could be read out of the text."""


def extract_fenced_json(text: str, *, shape: Shape) -> Any:
    """Return the JSON value of `shape` carried by `text`.

    Fenced blocks beat prose — a session narrates with brackets of its own
    before emitting the real payload — and they are tried LAST-first: a model
    that restates the requested shape, quotes an example, or shows a decoy block
    does so *before* its answer, so the final fenced block is the answer. Within
    each chunk the span taken is first opening bracket → LAST closing bracket,
    so a nested object or array inside the payload survives. The prose as a
    whole is the last resort, for output that never fenced anything.
    """
    open_b, close_b = _BRACKETS[shape]
    expected: type = list if shape == "array" else dict
    failure: FencedJsonError | None = None
    for chunk in [*reversed(_FENCE.findall(text)), text]:
        start, end = chunk.find(open_b), chunk.rfind(close_b)
        if start == -1 or end == -1 or end <= start:
            continue
        try:
            data = json.loads(chunk[start:end + 1])
        except json.JSONDecodeError as e:
            # Keep the LAST failure: candidates run best-first, so the message
            # the caller sees describes the widest thing we tried to read.
            failure = FencedJsonError(f"invalid JSON {shape}: {e}")
            continue
        if not isinstance(data, expected):
            failure = FencedJsonError(f"parsed JSON is not an {shape}")
            continue
        return data
    raise failure or FencedJsonError(f"no JSON {shape} found in output")
