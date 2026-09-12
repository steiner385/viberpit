"""The legacy-brand ratchet runs inside the lint-and-test gate.

`scripts/brand_ratchet.py` is the inventory half of the brand guard: where
`test_brand_wire_contracts.py` bans wire strings structurally, this counts every
remaining `ccswitch` spelling and demands each be claimed by a `.brand-ratchet.toml`
entry that names the issue removing it (or the design decision keeping it). A PR
that adds an unclaimed spelling, or grows a claimed category past its ceiling, fails
here — so a legacy reference can only enter the tree with a ticket attached.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = REPO_ROOT / "scripts" / "brand_ratchet.py"
_SPEC = importlib.util.spec_from_file_location("brand_ratchet", _SCRIPT_PATH)
assert _SPEC and _SPEC.loader
ratchet = importlib.util.module_from_spec(_SPEC)
# dataclasses resolve `from __future__ import annotations` through sys.modules.
sys.modules[_SPEC.name] = ratchet
_SPEC.loader.exec_module(ratchet)


@pytest.fixture(scope="module")
def config():
    return ratchet.load_registry()


@pytest.fixture(scope="module")
def report(config):
    return ratchet.scan(config)


def test_every_legacy_spelling_is_claimed(config, report):
    assert report.offenders == [], "\n" + ratchet.format_report(report, config)


def test_no_claimed_category_grew(config, report):
    grown = report.grown(config.entries)
    assert grown == [], "\n" + ratchet.format_report(report, config)


def test_every_entry_is_tracked_or_justified(config):
    """The registry loader already rejects an entry with neither an issue nor a reason;
    pin the rule at the test level too, so a loader refactor cannot quietly relax it."""
    for entry in config.entries:
        assert (entry.issue is None) != (entry.permanent is None), entry.id
        if entry.permanent is not None:
            assert entry.permanent.strip(), f"{entry.id}: permanent must say why"


def test_the_ratchet_would_catch_an_unclaimed_spelling(config):
    """Worth having only if it fires: a fresh product-name mention in production
    code is an offender, while the permanent dual-accept forms are claimed."""
    hits = ratchet.scan_text("server/new_module.py", "# ccswitch does the thing\n", config)
    assert [h.entry_id for h in hits] == [None]
    hits = ratchet.scan_text("server/new_module.py", 'M = "<!-- ccswitch:complete -->"\n', config)
    assert [h.entry_id for h in hits] == ["wire-dual-accept"]
    # Historical records are out of scope entirely, not merely claimed.
    assert not ratchet._in_scope("docs/superpowers/specs/x.md", config.exclude)


def test_registry_rejects_an_untracked_entry(tmp_path):
    bad = tmp_path / "r.toml"
    bad.write_text('[scan]\nneedles=["ccswitch"]\n[[allow]]\nid="x"\npattern="x"\nceiling=0\n')
    with pytest.raises(ratchet.RegistryError, match="exactly one of issue"):
        ratchet.load_registry(bad)


def test_update_rewrites_only_ceilings(tmp_path):
    reg = tmp_path / "r.toml"
    reg.write_text('# keep me\n[scan]\nneedles=["ccswitch"]\n\n'
                   '[[allow]]\nid = "a"\npattern = "ccswitch"\nissue = 1\nceiling = 5\n\n'
                   '[[allow]]\nid = "b"\npaths = ["never/*"]\npermanent = "why"\nceiling = 0\n')
    config = ratchet.load_registry(reg)
    report = ratchet.Report(counts={"a": 2, "b": 0}, offenders=[], hits=[])
    assert ratchet.rewrite_ceilings(report, reg) == ["a: 5 -> 2"]
    text = reg.read_text()
    assert text.startswith("# keep me\n")
    assert re.search(r'id = "a"\npattern = "ccswitch"\nissue = 1\nceiling = 2\n', text)
    assert 'permanent = "why"\nceiling = 0\n' in text
