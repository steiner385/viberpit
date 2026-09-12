#!/usr/bin/env python3
"""Legacy-brand ratchet — every remaining ``ccswitch`` spelling must be claimed.

The rebrand (docs/superpowers/specs/2026-09-06-viberpit-rebrand-design.md) is a
series of phases, and between phases the tree legitimately carries the old
spelling in places that cannot be renamed by search-and-replace: the Python
package, the env prefix, persisted browser keys, host paths, the permanent
dual-accept readers. The ratchet turns "we'll get to it" into a checked
inventory:

* ``.brand-ratchet.toml`` lists every *category* of legacy reference that is
  allowed to remain, each with the GitHub issue that will remove it (or a
  ``permanent`` reason citing the design decision that keeps it) and a
  ``ceiling`` — the count when the entry was last tightened.
* Any legacy spelling not claimed by an entry is an **offender**: rename it, or
  file an issue and add an entry. A category whose count grows past its
  ceiling fails the same way. A category whose count shrinks passes and prints
  a reminder to run ``--update`` so the ceiling follows it down.

Historical records (dated specs, plans, audits, recorded fixtures) are out of
scope entirely: rewriting them is churn, not progress.

Usage::

    python scripts/brand_ratchet.py            # report; exit 1 on any failure
    python scripts/brand_ratchet.py --update   # rewrite ceilings to today's counts
    python scripts/brand_ratchet.py --list ID  # print every hit claimed by entry ID

``server/tests/test_brand_ratchet.py`` runs the report inside the lint-and-test
gate, so a PR cannot merge with an unclaimed spelling or a grown category.
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / ".brand-ratchet.toml"


@dataclass(frozen=True)
class Entry:
    id: str
    pattern: re.Pattern[str] | None
    paths: tuple[str, ...]
    issue: int | None
    permanent: str | None
    ceiling: int

    def claims(self, rel_path: str, line: str, span: tuple[int, int]) -> bool:
        """Whether this entry accounts for the needle at *span* on *line*."""
        if self.paths and not any(fnmatch.fnmatch(rel_path, glob) for glob in self.paths):
            return False
        if self.pattern is None:
            return True
        start, end = span
        return any(m.start() < end and m.end() > start for m in self.pattern.finditer(line))


@dataclass(frozen=True)
class Hit:
    path: str
    line_no: int
    line: str
    entry_id: str | None


@dataclass
class Config:
    needles: re.Pattern[str]
    exclude: tuple[str, ...]
    entries: list[Entry] = field(default_factory=list)


@dataclass
class Report:
    counts: dict[str, int]
    offenders: list[Hit]
    hits: list[Hit]

    def grown(self, entries: list[Entry]) -> list[tuple[Entry, int]]:
        return [(e, self.counts[e.id]) for e in entries if self.counts[e.id] > e.ceiling]

    def shrunk(self, entries: list[Entry]) -> list[tuple[Entry, int]]:
        return [(e, self.counts[e.id]) for e in entries if self.counts[e.id] < e.ceiling]

    @property
    def ok(self) -> bool:
        return not self.offenders


class RegistryError(ValueError):
    """The registry is malformed — fail loudly rather than silently skip a rule."""


def load_registry(path: Path = REGISTRY_PATH) -> Config:
    raw = tomllib.loads(path.read_text())
    scan = raw.get("scan", {})
    needles = scan.get("needles")
    if not needles:
        raise RegistryError("[scan].needles must list at least one spelling")
    exclude = tuple(scan.get("exclude", ()))
    config = Config(
        needles=re.compile("|".join(re.escape(n) for n in needles), re.IGNORECASE),
        exclude=exclude,
    )
    seen: set[str] = set()
    for block in raw.get("allow", []):
        entry_id = block.get("id")
        if not entry_id or entry_id in seen:
            raise RegistryError(f"every [[allow]] needs a unique id (got {entry_id!r})")
        seen.add(entry_id)
        issue, permanent = block.get("issue"), block.get("permanent")
        if (issue is None) == (permanent is None):
            raise RegistryError(f"{entry_id}: set exactly one of issue = <int> or permanent = <reason>")
        if issue is not None and (not isinstance(issue, int) or issue <= 0):
            raise RegistryError(f"{entry_id}: issue must be a positive GitHub issue number")
        if permanent is not None and not str(permanent).strip():
            raise RegistryError(f"{entry_id}: permanent must say why")
        pattern, paths = block.get("pattern"), tuple(block.get("paths", ()))
        if pattern is None and not paths:
            raise RegistryError(f"{entry_id}: needs a pattern, paths, or both")
        ceiling = block.get("ceiling")
        if not isinstance(ceiling, int) or ceiling < 0:
            raise RegistryError(f"{entry_id}: ceiling must be a non-negative int")
        config.entries.append(Entry(
            id=entry_id,
            pattern=re.compile(pattern) if pattern is not None else None,
            paths=paths, issue=issue, permanent=permanent, ceiling=ceiling,
        ))
    return config


def tracked_files(root: Path = REPO_ROOT) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                         capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]


def _in_scope(rel_path: str, exclude: tuple[str, ...]) -> bool:
    return not any(rel_path == ex or rel_path.startswith(ex) for ex in exclude)


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except (OSError, IsADirectoryError):
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_text(rel_path: str, text: str, config: Config) -> list[Hit]:
    hits: list[Hit] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for m in config.needles.finditer(line):
            claimed = next((e.id for e in config.entries
                            if e.claims(rel_path, line, m.span())), None)
            hits.append(Hit(rel_path, line_no, line.strip(), claimed))
    return hits


def scan(config: Config, root: Path = REPO_ROOT, files: list[str] | None = None) -> Report:
    hits: list[Hit] = []
    for rel_path in (files if files is not None else tracked_files(root)):
        if not _in_scope(rel_path, config.exclude):
            continue
        text = _read_text(root / rel_path)
        if text is None:
            continue
        hits.extend(scan_text(rel_path, text, config))
    counts = {e.id: 0 for e in config.entries}
    for h in hits:
        if h.entry_id is not None:
            counts[h.entry_id] += 1
    return Report(counts=counts, offenders=[h for h in hits if h.entry_id is None], hits=hits)


def rewrite_ceilings(report: Report, path: Path = REGISTRY_PATH) -> list[str]:
    """Set every entry's ceiling to its current count, preserving comments.

    tomllib has no writer, so the file is edited textually: each ``[[allow]]``
    block is located by its ``id`` line and its ``ceiling`` line replaced.
    """
    text = path.read_text()
    changed: list[str] = []
    blocks = text.split("[[allow]]")
    for i, block in enumerate(blocks[1:], start=1):
        id_m = re.search(r'^id\s*=\s*"([^"]+)"', block, re.MULTILINE)
        if not id_m or id_m.group(1) not in report.counts:
            continue
        entry_id = id_m.group(1)
        new = report.counts[entry_id]
        ceiling_m = re.search(r"^ceiling\s*=\s*(\d+)", block, re.MULTILINE)
        if not ceiling_m:
            raise RegistryError(f"{entry_id}: no ceiling line to rewrite")
        old = int(ceiling_m.group(1))
        if old != new:
            blocks[i] = block[:ceiling_m.start(1)] + str(new) + block[ceiling_m.end(1):]
            changed.append(f"{entry_id}: {old} -> {new}" + ("  (RAISED)" if new > old else ""))
    path.write_text("[[allow]]".join(blocks))
    return changed


def format_report(report: Report, config: Config) -> str:
    lines: list[str] = []
    if report.offenders:
        lines.append(f"UNCLAIMED legacy brand references ({len(report.offenders)}):")
        lines.append("  rename them, or file an issue and add a [[allow]] entry to .brand-ratchet.toml")
        for h in report.offenders:
            lines.append(f"  {h.path}:{h.line_no}: {h.line[:140]}")
        lines.append("")
    grown = report.grown(config.entries)
    if grown:
        lines.append("GREW past ceiling:")
        for e, n in grown:
            lines.append(f"  {e.id}: {n} > {e.ceiling}  (#{e.issue})" if e.issue else
                         f"  {e.id}: {n} > {e.ceiling}  (permanent)")
        lines.append("")
    lines.append(f"{'entry':<28}{'count':>7}{'ceiling':>9}  tracked by")
    for e in config.entries:
        tracked = f"#{e.issue}" if e.issue else "permanent"
        lines.append(f"{e.id:<28}{report.counts[e.id]:>7}{e.ceiling:>9}  {tracked}")
    shrunk = report.shrunk(config.entries)
    if shrunk:
        lines.append("")
        lines.append("shrunk — run `python scripts/brand_ratchet.py --update` to lower: "
                     + ", ".join(f"{e.id} ({n}<{e.ceiling})" for e, n in shrunk))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--update", action="store_true", help="rewrite ceilings to current counts")
    parser.add_argument("--list", metavar="ID", help="print every hit claimed by entry ID")
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    args = parser.parse_args(argv)

    config = load_registry(args.registry)
    report = scan(config)
    if args.list:
        for h in report.hits:
            if h.entry_id == args.list:
                print(f"{h.path}:{h.line_no}: {h.line[:160]}")
        return 0
    if args.update:
        for change in rewrite_ceilings(report, args.registry):
            print(change)
    print(format_report(report, config))
    grown = report.grown(config.entries) if not args.update else []
    return 0 if report.ok and not grown else 1


if __name__ == "__main__":
    sys.exit(main())
