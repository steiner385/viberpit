"""Release-milestone ladder definition: strict YAML parser + measure-kind registry.

Pure code, no DB. The parsed ladder is stored as JSON (``to_json`` / ``from_json``) in
``milestone_ladders.definition``. Product-agnostic: nothing here names a product.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

# Where pm-sync reads a product's ladder definition, on its primary repo's default branch.
MILESTONES_PATH = "docs/product/milestones.yaml"
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_REF_RE = re.compile(r"^(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(?P<n>[0-9]+)$")
OPS = frozenset({"eq", "ne", "lt", "le", "gt", "ge"})
RESERVED_MILESTONE_KEYS = frozenset({"numbers"})


class MilestoneDefError(ValueError):
    """The definition file is invalid."""


@dataclass(frozen=True)
class MeasureKindSpec:
    required: frozenset[str]
    optional: frozenset[str]
    default_max_age_s: int | None
    gate_ok: bool


_H = 3600
MEASURE_KINDS: dict[str, MeasureKindSpec] = {
    "sub_issues": MeasureKindSpec(frozenset(), frozenset({"epic"}), _H, True),
    "issue_count": MeasureKindSpec(frozenset({"query"}), frozenset({"repo"}), _H, True),
    "refs_closed": MeasureKindSpec(frozenset({"refs"}), frozenset(), _H, True),
    "workflow_streak": MeasureKindSpec(
        frozenset({"workflow"}), frozenset({"branch", "events"}), 36 * _H, True
    ),
    "ci_metric": MeasureKindSpec(frozenset({"workflow", "metric"}), frozenset(), 36 * _H, True),
    "ladder_leaves": MeasureKindSpec(frozenset(), frozenset(), _H, False),
    "manual": MeasureKindSpec(frozenset(), frozenset(), None, True),
}
# Kinds whose gate condition is met by "all closed" when op/target are absent.
_ALL_CLOSED_KINDS = frozenset({"sub_issues", "refs_closed"})


@dataclass(frozen=True)
class Ref:
    repo: str
    number: int

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass(frozen=True)
class Measure:
    kind: str
    params: Mapping[str, Any]
    op: str | None
    target: float | None
    max_age_s: int | None


@dataclass(frozen=True)
class Condition:
    key: str
    label: str
    measure: Measure


@dataclass(frozen=True)
class Platform:
    name: str
    off: bool = False
    note: str | None = None


@dataclass(frozen=True)
class MilestoneDef:
    key: str
    name: str
    label: str | None
    audience: str | None
    platforms: tuple[Platform, ...]
    epics: tuple[Ref, ...]
    refs: tuple[Ref, ...]
    gate: tuple[Condition, ...]


@dataclass(frozen=True)
class LadderDef:
    version: int
    title: str
    spec: str | None
    start: str | None
    milestones: tuple[MilestoneDef, ...]
    numbers: tuple[Condition, ...]


def parse_ref(s: str, *, primary_repo: str) -> Ref:
    m = _REF_RE.match(s.strip()) if isinstance(s, str) else None
    if not m:
        raise MilestoneDefError(f"bad ref {s!r}: expected '#N' or 'owner/name#N'")
    return Ref(m["repo"] or primary_repo, int(m["n"]))


# ---------------------------------------------------------------- parsing helpers


def _mapping(v: Any, where: str, allowed: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(v, Mapping):
        raise MilestoneDefError(f"{where}: expected a mapping")
    for k in v:
        if k not in allowed:
            raise MilestoneDefError(f"{where}: unknown key {k!r}")
    return v


def _list(v: Any, where: str) -> list[Any]:
    if v is None:
        return []
    if not isinstance(v, list):
        raise MilestoneDefError(f"{where}: expected a list")
    return v


def _str(v: Any, where: str, *, required: bool = False) -> str | None:
    if v is None:
        if required:
            raise MilestoneDefError(f"{where}: required")
        return None
    if not isinstance(v, str) or not v.strip():
        raise MilestoneDefError(f"{where}: expected a non-empty string")
    return v


def _key(v: Any, where: str) -> str:
    s = _str(v, f"{where} key", required=True)
    assert s is not None
    if not KEY_RE.match(s):
        raise MilestoneDefError(f"{where} key {s!r} must match [a-z0-9][a-z0-9-]*")
    return s


def _refs(v: Any, where: str, primary_repo: str) -> tuple[Ref, ...]:
    return tuple(parse_ref(x, primary_repo=primary_repo) for x in _list(v, where))


def _platform(v: Any, where: str) -> Platform:
    if isinstance(v, str) and v.strip():
        return Platform(v)
    if isinstance(v, Mapping):
        # YAML 1.1 reads the bare key ``off`` as boolean False; the schema spells it ``off``.
        v = {("off" if k is False else k): val for k, val in v.items()}
    m = _mapping(v, where, frozenset({"name", "off", "note"}))
    off = m.get("off", False)
    if not isinstance(off, bool):
        raise MilestoneDefError(f"{where}: off must be true or false")
    return Platform(_str(m.get("name"), f"{where} name", required=True) or "", off, _str(m.get("note"), f"{where} note"))


def _number(v: Any, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise MilestoneDefError(f"{where}: expected a number")
    return v


_COMMON_MEASURE_KEYS = frozenset({"kind", "op", "target", "max_age"})


def _measure(v: Any, where: str, primary_repo: str, *, in_numbers: bool) -> Measure:
    if not isinstance(v, Mapping):
        raise MilestoneDefError(f"{where}: measure must be a mapping")
    kind = v.get("kind")
    spec = MEASURE_KINDS.get(kind) if isinstance(kind, str) else None
    if spec is None or not isinstance(kind, str):
        raise MilestoneDefError(f"{where}: unknown measure kind {kind!r}")
    _mapping(v, f"{where} measure", _COMMON_MEASURE_KEYS | spec.required | spec.optional)
    for name in sorted(spec.required):
        if name not in v:
            raise MilestoneDefError(f"{where}: measure kind {kind!r} requires {name!r}")
    if not spec.gate_ok and not in_numbers:
        raise MilestoneDefError(f"{where}: {kind} is allowed only in numbers")
    if kind == "ladder_leaves" and ("op" in v or "target" in v):
        raise MilestoneDefError(f"{where}: ladder_leaves takes no op or target")
    if kind == "manual" and ("op" in v or "target" in v):
        raise MilestoneDefError(f"{where}: manual takes no op or target")

    op = v.get("op")
    if kind == "workflow_streak" and op is None:
        op = "ge"
    if op is not None and op not in OPS:
        raise MilestoneDefError(f"{where}: op must be one of {' '.join(sorted(OPS))}")
    target = _number(v["target"], f"{where} target") if "target" in v else None
    has_op_target = op is not None and target is not None
    if (v.get("op") is not None or "target" in v) and not (op is not None and target is not None):
        raise MilestoneDefError(f"{where}: {kind} needs op and target together")
    if (
        not in_numbers
        and kind not in _ALL_CLOSED_KINDS
        and kind != "manual"
        and not has_op_target
    ):
        raise MilestoneDefError(f"{where}: {kind} needs op and target")

    max_age = v.get("max_age")
    if max_age is not None and (isinstance(max_age, bool) or not isinstance(max_age, int) or max_age <= 0):
        raise MilestoneDefError(f"{where}: max_age must be a positive integer (seconds)")

    params: dict[str, Any] = {}
    for name in sorted(spec.required | spec.optional):
        if name not in v:
            continue
        val = v[name]
        if name == "refs":
            val = _refs(val, f"{where} refs", primary_repo)
            if not val:
                raise MilestoneDefError(f"{where}: refs must not be empty")
        elif name == "epic":
            val = parse_ref(val, primary_repo=primary_repo)
        elif name == "events":
            val = tuple(_str(x, f"{where} events", required=True) for x in _list(val, f"{where} events"))
        else:
            val = _str(val, f"{where} {name}", required=True)
        params[name] = val
    return Measure(kind, params, op, target, max_age)


def _conditions(v: Any, where: str, primary_repo: str, *, in_numbers: bool) -> tuple[Condition, ...]:
    out: list[Condition] = []
    seen: set[str] = set()
    for i, raw in enumerate(_list(v, where)):
        w = f"{where}[{i}]"
        m = _mapping(raw, w, frozenset({"key", "label", "measure"}))
        key = _key(m.get("key"), w)
        if key in seen:
            raise MilestoneDefError(f"{where}: duplicate condition key {key!r}")
        seen.add(key)
        label = _str(m.get("label"), f"{w} label", required=True) or ""
        out.append(Condition(key, label, _measure(m.get("measure"), f"{w} ({key})", primary_repo, in_numbers=in_numbers)))
    return tuple(out)


def _milestone(raw: Any, i: int, primary_repo: str) -> MilestoneDef:
    w = f"milestones[{i}]"
    m = _mapping(
        raw, w, frozenset({"key", "name", "label", "audience", "platforms", "epics", "refs", "gate"})
    )
    key = _key(m.get("key"), w)
    if key in RESERVED_MILESTONE_KEYS:
        raise MilestoneDefError(f"{w}: key {key!r} is reserved")
    w = f"milestone {key!r}"
    return MilestoneDef(
        key=key,
        name=_str(m.get("name"), f"{w} name", required=True) or "",
        label=_str(m.get("label"), f"{w} label"),
        audience=_str(m.get("audience"), f"{w} audience"),
        platforms=tuple(_platform(p, f"{w} platforms") for p in _list(m.get("platforms"), f"{w} platforms")),
        epics=_refs(m.get("epics"), f"{w} epics", primary_repo),
        refs=_refs(m.get("refs"), f"{w} refs", primary_repo),
        gate=_conditions(m.get("gate"), f"{w} gate", primary_repo, in_numbers=False),
    )


def parse_ladder(text: str, *, primary_repo: str) -> LadderDef:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise MilestoneDefError(f"YAML: {exc}") from exc
    top = _mapping(doc, "ladder", frozenset({"version", "title", "spec", "start", "milestones", "numbers"}))
    if top.get("version") != 1 or isinstance(top.get("version"), bool):
        raise MilestoneDefError("unsupported version: expected 1")
    milestones: list[MilestoneDef] = []
    for i, raw in enumerate(_list(top.get("milestones"), "milestones")):
        ms = _milestone(raw, i, primary_repo)
        if any(x.key == ms.key for x in milestones):
            raise MilestoneDefError(f"duplicate milestone key {ms.key!r}")
        milestones.append(ms)
    start = _str(top.get("start"), "start")
    if start is not None and start not in {m.key for m in milestones}:
        raise MilestoneDefError(f"start {start!r} is not a milestone key")
    return LadderDef(
        version=1,
        title=_str(top.get("title"), "title", required=True) or "",
        spec=_str(top.get("spec"), "spec"),
        start=start,
        milestones=tuple(milestones),
        numbers=_conditions(top.get("numbers"), "numbers", primary_repo, in_numbers=True),
    )


# ---------------------------------------------------------------- JSON round-trip


def _params_to_json(params: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        if isinstance(v, Ref):
            out[k] = v.key
        elif isinstance(v, tuple):
            out[k] = [x.key if isinstance(x, Ref) else x for x in v]
        else:
            out[k] = v
    return out


def _params_from_json(params: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in params.items():
        if k == "epic":
            out[k] = parse_ref(v, primary_repo="")
        elif k == "refs":
            out[k] = tuple(parse_ref(x, primary_repo="") for x in v)
        elif k == "events":
            out[k] = tuple(v)
        else:
            out[k] = v
    return out


def _cond_to_json(c: Condition) -> dict[str, Any]:
    m = c.measure
    return {
        "key": c.key,
        "label": c.label,
        "measure": {
            "kind": m.kind,
            "params": _params_to_json(m.params),
            "op": m.op,
            "target": m.target,
            "max_age_s": m.max_age_s,
        },
    }


def _cond_from_json(d: Mapping[str, Any]) -> Condition:
    m = d["measure"]
    return Condition(
        d["key"],
        d["label"],
        Measure(m["kind"], _params_from_json(m["params"]), m["op"], m["target"], m["max_age_s"]),
    )


def to_json(ladder: LadderDef) -> dict[str, Any]:
    return {
        "version": ladder.version,
        "title": ladder.title,
        "spec": ladder.spec,
        "start": ladder.start,
        "milestones": [
            {
                "key": m.key,
                "name": m.name,
                "label": m.label,
                "audience": m.audience,
                "platforms": [{"name": p.name, "off": p.off, "note": p.note} for p in m.platforms],
                "epics": [r.key for r in m.epics],
                "refs": [r.key for r in m.refs],
                "gate": [_cond_to_json(c) for c in m.gate],
            }
            for m in ladder.milestones
        ],
        "numbers": [_cond_to_json(c) for c in ladder.numbers],
    }


def from_json(d: Mapping[str, Any]) -> LadderDef:
    return LadderDef(
        version=d["version"],
        title=d["title"],
        spec=d["spec"],
        start=d["start"],
        milestones=tuple(
            MilestoneDef(
                key=m["key"],
                name=m["name"],
                label=m["label"],
                audience=m["audience"],
                platforms=tuple(Platform(p["name"], p["off"], p["note"]) for p in m["platforms"]),
                epics=tuple(parse_ref(r, primary_repo="") for r in m["epics"]),
                refs=tuple(parse_ref(r, primary_repo="") for r in m["refs"]),
                gate=tuple(_cond_from_json(c) for c in m["gate"]),
            )
            for m in d["milestones"]
        ),
        numbers=tuple(_cond_from_json(c) for c in d["numbers"]),
    )
