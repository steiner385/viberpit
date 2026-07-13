"""Tests for server/pipeline/ci_graph.py — CI-graph derivation + diff (SPEC §4).

Line renderings are pinned VERBATIM against plotroom's workflow-impact.ts:24-47
(the glyphs `+` / `−` / `unset` govern; extracted into phase3-plotroom-spec §4).
"""
from server.pipeline.ci_graph import derive_ci_graph, diff_ci_graphs

# A workflow whose rollup job `ci` needs three jobs, one of which needs a fourth.
# Timeouts declared on a subset; `ci` itself carries none.
_YAML = """
name: CI
on: [push]
jobs:
  ci:
    needs: [build, test]
    runs-on: ubuntu-latest
  build:
    needs: [setup]
    runs-on: ubuntu-latest
    timeout-minutes: 20
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 15
  setup:
    runs-on: ubuntu-latest
  unrelated:
    runs-on: ubuntu-latest
    timeout-minutes: 99
"""


def test_derive_bfs_closure_from_rollup():
    g = derive_ci_graph(_YAML, "ci")
    assert g is not None
    # Closure is ci → {build, test} → {setup}; `unrelated` is NOT in the needs
    # closure so it is excluded.
    assert set(g["prefixes"]) == {"ci", "build", "test", "setup"}
    assert "unrelated" not in g["prefixes"]


def test_derive_captures_timeout_minutes():
    g = derive_ci_graph(_YAML, "ci")
    assert g is not None
    assert g["nodes"]["build"]["timeoutMinutes"] == 20
    assert g["nodes"]["test"]["timeoutMinutes"] == 15
    # No timeout-minutes declared → None.
    assert g["nodes"]["ci"]["timeoutMinutes"] is None
    assert g["nodes"]["setup"]["timeoutMinutes"] is None


def test_derive_uses_job_display_name_as_prefix():
    y = """
name: CI
jobs:
  ci:
    needs: [lint]
  lint:
    name: Lint & Format
    timeout-minutes: 5
"""
    g = derive_ci_graph(y, "ci")
    assert g is not None
    assert "Lint & Format" in g["prefixes"]
    assert g["nodes"]["Lint & Format"]["timeoutMinutes"] == 5


def test_derive_malformed_yaml_returns_none():
    assert derive_ci_graph("::: not valid yaml : : :\n  - [", "ci") is None


def test_derive_missing_rollup_job_degrades_to_rollup_only():
    # Valid YAML, `jobs:` present but no `ci` job → rollup-only graph.
    y = "name: CI\njobs:\n  other:\n    runs-on: ubuntu-latest\n"
    g = derive_ci_graph(y, "ci")
    assert g is not None
    assert g["prefixes"] == ["ci"]
    assert g["nodes"]["ci"]["timeoutMinutes"] is None


def test_derive_missing_jobs_map_degrades_to_rollup_only():
    g = derive_ci_graph("name: CI\non: [push]\n", "ci")
    assert g is not None
    assert g["prefixes"] == ["ci"]


def test_derive_is_cycle_safe():
    y = """
name: CI
jobs:
  ci:
    needs: [a]
  a:
    needs: [b]
  b:
    needs: [a]
"""
    g = derive_ci_graph(y, "ci")
    assert g is not None
    assert set(g["prefixes"]) == {"ci", "a", "b"}


# --- diff_ci_graphs: line renderings pinned VERBATIM (SPEC §4 / TS 24-47) -----


def _graph(prefixes, nodes=None):
    return {"prefixes": list(prefixes), "nodes": nodes or {}}


def test_diff_none_when_identical():
    base = _graph(["ci", "build"])
    head = _graph(["ci", "build"])
    assert diff_ci_graphs(base, head) is None


def test_diff_join_line_verbatim():
    base = _graph(["ci"])
    head = _graph(["ci", "android-smoke"])
    out = diff_ci_graphs(base, head)
    assert out is not None
    assert "+ android-smoke joins the merge_group gate" in out["summary"]


def test_diff_leave_line_verbatim():
    base = _graph(["ci", "android-smoke"])
    head = _graph(["ci"])
    out = diff_ci_graphs(base, head)
    assert out is not None
    assert "− android-smoke leaves the merge_group gate" in out["summary"]


def test_diff_size_grows_line_verbatim():
    base = _graph(["ci"])
    head = _graph(["ci", "a"])
    out = diff_ci_graphs(base, head)
    assert out is not None
    assert "required-check set grows by 1: 1 → 2 checks" in out["summary"]


def test_diff_size_shrinks_line_verbatim():
    base = _graph(["ci", "a", "b"])
    head = _graph(["ci"])
    out = diff_ci_graphs(base, head)
    assert out is not None
    assert "required-check set shrinks by 2: 3 → 1 checks" in out["summary"]


def test_diff_timeout_change_line_verbatim():
    base = _graph(["ci", "build"], {"build": {"timeoutMinutes": 20}})
    head = _graph(["ci", "build"], {"build": {"timeoutMinutes": 30}})
    out = diff_ci_graphs(base, head)
    assert out is not None
    assert "build timeout-minutes 20m → 30m" in out["summary"]


def test_diff_timeout_unset_rendering():
    base = _graph(["ci", "build"], {"build": {"timeoutMinutes": None}})
    head = _graph(["ci", "build"], {"build": {"timeoutMinutes": 30}})
    out = diff_ci_graphs(base, head)
    assert out is not None
    assert "build timeout-minutes unset → 30m" in out["summary"]


def test_diff_timeout_skipped_for_joining_jobs():
    # `build` joins the gate (not in base) → the join line covers it; no timeout
    # line even though the node has a timeout value.
    base = _graph(["ci"], {})
    head = _graph(["ci", "build"], {"build": {"timeoutMinutes": 30}})
    out = diff_ci_graphs(base, head)
    assert out is not None
    assert "+ build joins the merge_group gate" in out["summary"]
    assert not any("timeout-minutes" in line for line in out["summary"])


def test_diff_ordering_joins_then_leaves_then_size_then_timeouts():
    base = _graph(["ci", "gone", "keep"], {"keep": {"timeoutMinutes": 10}})
    head = _graph(["ci", "new", "keep"], {"keep": {"timeoutMinutes": 12}})
    out = diff_ci_graphs(base, head)
    assert out is not None
    assert out["summary"] == [
        "+ new joins the merge_group gate",
        "− gone leaves the merge_group gate",
        "keep timeout-minutes 10m → 12m",
    ]
