"""Tests for the metrics engine (metrics/engine.py, SPEC F5).

Uses two fixtures:

- ``tests/fixtures/metrics_sample`` (new, purpose-built for this file): a
  tiny 5-file python package whose LOC/CC/params/fan-in/fan-out values were
  computed by hand (see the docstring of each assertion group below) so
  every number in these tests can be independently re-derived by reading
  the fixture source.
- ``tests/fixtures/sample_project`` (existing, shared with the core/parser
  test suite): used as a cross-language smoke test that every scanned file
  gets a metrics entry and nothing crashes, including the intentionally
  broken python file.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from codeanalyzer.core.pipeline import analyze_project
from codeanalyzer.metrics.engine import compute_metrics

METRICS_FIXTURE = Path(__file__).parent / "fixtures" / "metrics_sample"
SAMPLE_FIXTURE = Path(__file__).parent / "fixtures" / "sample_project"
LANG_PROBE_FIXTURE = Path(__file__).parent / "fixtures" / "metrics_lang_probe"

_FILE_METRIC_KEYS = {
    "loc_total", "loc_code", "loc_comment", "loc_blank",
    "functions", "classes", "cc_total", "cc_max", "mi",
    "fan_in", "fan_out", "fan_out_external",
    "pagerank", "betweenness", "closeness", "degree_centrality",
}
_SYMBOL_METRIC_KEYS = {"loc", "cc", "params"}
_PROJECT_KEYS = {
    "files", "loc_total", "loc_code", "loc_comment", "functions", "classes",
    "cc_avg", "cc_max", "mi_avg", "cycles_count", "languages", "test",
}


@pytest.fixture(scope="module")
def metrics_result():
    r = analyze_project(METRICS_FIXTURE, keep_sources=True)
    compute_metrics(r, r._sources)
    return r


# ---------------------------------------------------------------------------
# LOC: hand-computed from tests/fixtures/metrics_sample/pkg/*.py
# ---------------------------------------------------------------------------

def test_loc_calc_py(metrics_result):
    # calc.py: 14 physical lines.
    #   code(10): 1 (docstring), 2 (import), 5 (def calc), 6 (docstring),
    #             7 (if ... # comment -> counts as code), 8 (for), 9 (pass),
    #             10 (return), 13 (def helper), 14 (return)
    #   comment(1): line 4 (pure "# top-level comment ..." line)
    #   blank(3): lines 3, 11, 12
    m = metrics_result.metrics["files"]["pkg/calc.py"]
    assert m["loc_total"] == 14
    assert m["loc_code"] == 10
    assert m["loc_comment"] == 1
    assert m["loc_blank"] == 3


def test_loc_util_app_test_files(metrics_result):
    # util.py / app.py / test_calc.py all share the same shape:
    # docstring, import, blank, blank, def, return -> 6 total, 4 code, 2 blank
    for fid in ("pkg/util.py", "pkg/app.py", "pkg/test_calc.py"):
        m = metrics_result.metrics["files"][fid]
        assert m["loc_total"] == 6
        assert m["loc_code"] == 4
        assert m["loc_comment"] == 0
        assert m["loc_blank"] == 2


def test_loc_empty_file(metrics_result):
    m = metrics_result.metrics["files"]["pkg/__init__.py"]
    assert m == {
        "loc_total": 0, "loc_code": 0, "loc_comment": 0, "loc_blank": 0,
        "functions": 0, "classes": 0, "cc_total": 0, "cc_max": 0,
        "mi": 100.0,  # guarded: empty file -> mi == 100
        "fan_in": 0, "fan_out": 0, "fan_out_external": 0,
        # __init__.py has no import edges at all -> isolated in the network
        # graph -> 0.0 for every network metric (see metrics/network.py).
        "pagerank": 0.0, "betweenness": 0.0, "closeness": 0.0,
        "degree_centrality": 0.0,
    }


# ---------------------------------------------------------------------------
# CC / params: hand-computed decision points
# ---------------------------------------------------------------------------

def test_cc_known_function_if_for_and(metrics_result):
    # def calc(a, b):
    #     if a > 0 and b > 0:      -> +1 (if) +1 (and)
    #         for i in range(a):   -> +1 (for)
    #             pass
    #     return a + b
    # cc = 1 (base) + 1 (if) + 1 (and) + 1 (for) = 4
    sym = metrics_result.metrics["symbols"]["pkg/calc.py::calc"]
    assert sym["cc"] == 4
    assert sym["params"] == 2
    assert sym["loc"] == 6  # lines 5-10 inclusive


def test_cc_no_decision_points_is_one(metrics_result):
    for sym_id in (
        "pkg/calc.py::helper",
        "pkg/util.py::read_env",
        "pkg/app.py::main",
        "pkg/test_calc.py::test_calc_adds",
    ):
        assert metrics_result.metrics["symbols"][sym_id]["cc"] == 1


def test_params_counts(metrics_result):
    sm = metrics_result.metrics["symbols"]
    assert sm["pkg/util.py::read_env"]["params"] == 1
    assert sm["pkg/app.py::main"]["params"] == 0
    assert sm["pkg/test_calc.py::test_calc_adds"]["params"] == 0
    assert sm["pkg/calc.py::helper"]["params"] == 0


def test_file_cc_total_and_max(metrics_result):
    m = metrics_result.metrics["files"]["pkg/calc.py"]
    # calc() cc=4, helper() cc=1 -> total 5, max 4
    assert m["cc_total"] == 5
    assert m["cc_max"] == 4
    assert m["functions"] == 2
    assert m["classes"] == 0


def test_cc_cross_language_js_if_for_and():
    # Cross-language sanity check (SPEC F5 decision-point set is shared
    # across languages): same if + && + for shape as the python fixture,
    # written in JS -- verifies CC counting isn't python-only.
    #
    # function calc(a, b) {
    #   if (a > 0 && b > 0) {   -> +1 (if) +1 (&&)
    #     for (...) { ... }     -> +1 (for)
    #   }
    #   return a + b;
    # }
    r = analyze_project(LANG_PROBE_FIXTURE, keep_sources=True)
    compute_metrics(r, r._sources)
    sm = r.metrics["symbols"]
    assert sm["probe.js::calc"]["cc"] == 4
    assert sm["probe.js::calc"]["params"] == 2
    assert sm["probe.js::plain"]["cc"] == 1
    assert sm["probe.js::plain"]["params"] == 1


# ---------------------------------------------------------------------------
# fan-in / fan-out: known import chain
#   util.py  <-- calc.py <-- app.py
#                        <-- test_calc.py
#   util.py --> os (external)
# ---------------------------------------------------------------------------

def test_fan_in_out(metrics_result):
    files = metrics_result.metrics["files"]
    assert files["pkg/util.py"]["fan_in"] == 1
    assert files["pkg/util.py"]["fan_out"] == 0
    assert files["pkg/util.py"]["fan_out_external"] == 1  # import os

    assert files["pkg/calc.py"]["fan_in"] == 2  # app.py, test_calc.py
    assert files["pkg/calc.py"]["fan_out"] == 1  # -> util.py
    assert files["pkg/calc.py"]["fan_out_external"] == 0

    assert files["pkg/app.py"]["fan_in"] == 0
    assert files["pkg/app.py"]["fan_out"] == 1

    assert files["pkg/__init__.py"]["fan_in"] == 0
    assert files["pkg/__init__.py"]["fan_out"] == 0


# ---------------------------------------------------------------------------
# MI: not hand-derivable exactly (log-based formula) but must be sane
# ---------------------------------------------------------------------------

def test_mi_in_range(metrics_result):
    for fid, m in metrics_result.metrics["files"].items():
        assert 0 < m["mi"] <= 100, f"{fid}: mi={m['mi']}"


# ---------------------------------------------------------------------------
# project aggregates: sums must be internally consistent with the hand
# computed per-file numbers above.
# ---------------------------------------------------------------------------

def test_project_aggregates(metrics_result):
    proj = metrics_result.metrics["project"]
    assert proj["files"] == 5
    assert proj["loc_total"] == 14 + 6 + 6 + 6 + 0
    assert proj["loc_code"] == 10 + 4 + 4 + 4 + 0
    assert proj["loc_comment"] == 1
    assert proj["functions"] == 5  # calc, helper, read_env, main, test_calc_adds
    assert proj["classes"] == 0
    # cc values: calc=4, helper=1, read_env=1, main=1, test_calc_adds=1
    assert proj["cc_avg"] == pytest.approx((4 + 1 + 1 + 1 + 1) / 5)
    assert proj["cc_max"] == 4
    assert proj["cycles_count"] == 0
    assert proj["languages"] == {"python": {"files": 5, "loc_code": 22}}
    assert proj["test"] == {"files": 1, "loc_code": 4}


def test_test_files_split(metrics_result):
    assert metrics_result.metrics["test_files"] == ["pkg/test_calc.py"]
    fids = {f.id for f in metrics_result.files if f.is_test}
    assert fids == {"pkg/test_calc.py"}


def test_cycles_none_for_acyclic_project(metrics_result):
    assert metrics_result.metrics["cycles"] == []


# ---------------------------------------------------------------------------
# shape / contract: exact key names matter (binding contract with the HTML
# report template).
# ---------------------------------------------------------------------------

def test_metrics_shape_matches_contract(metrics_result):
    m = metrics_result.metrics
    assert set(m.keys()) >= {
        "files", "symbols", "project", "cycles", "definitions", "test_files",
        "network",
    }
    for fid, entry in m["files"].items():
        assert set(entry.keys()) == _FILE_METRIC_KEYS, fid
    for sid, entry in m["symbols"].items():
        assert set(entry.keys()) == _SYMBOL_METRIC_KEYS, sid
    assert set(m["project"].keys()) == _PROJECT_KEYS
    assert isinstance(m["cycles"], list)
    assert isinstance(m["test_files"], list)
    assert isinstance(m["definitions"], dict)
    assert all(isinstance(v, str) for v in m["definitions"].values())


def test_symbols_only_cover_functions_and_methods(metrics_result):
    sym_by_id = {s.id: s for s in metrics_result.symbols}
    for sid in metrics_result.metrics["symbols"]:
        assert sym_by_id[sid].kind in ("function", "method")
    # module/class symbols must NOT appear in metrics["symbols"]
    non_func_ids = {s.id for s in metrics_result.symbols
                    if s.kind not in ("function", "method")}
    assert not (non_func_ids & set(metrics_result.metrics["symbols"]))


# ---------------------------------------------------------------------------
# cycles: compute_metrics must keep/reuse the pipeline's precomputed
# file-level SCCs (not silently diverge from them).
# ---------------------------------------------------------------------------

def test_cycles_reuses_pipeline_precomputed_value():
    r = analyze_project(SAMPLE_FIXTURE, keep_sources=True)
    pre = list(r.metrics["cycles"])
    assert pre == [["py/cyclic_a.py", "py/cyclic_b.py"]]
    compute_metrics(r, r._sources)
    assert r.metrics["cycles"] == pre


def test_cycles_recomputed_when_absent():
    # Simulate a caller that didn't go through analyze_project's setdefault
    # (or explicitly cleared it): compute_metrics must fall back to
    # deriving the same cycles itself rather than leaving the key unset.
    r = analyze_project(SAMPLE_FIXTURE, keep_sources=True)
    expected = list(r.metrics["cycles"])
    del r.metrics["cycles"]
    compute_metrics(r, r._sources)
    assert r.metrics["cycles"] == expected


# ---------------------------------------------------------------------------
# smoke test: cross-language project, including a partially-broken file,
# must produce a metrics entry for every scanned file without crashing.
# ---------------------------------------------------------------------------

def test_smoke_every_file_has_metrics():
    r = analyze_project(SAMPLE_FIXTURE, keep_sources=True)
    compute_metrics(r, r._sources)
    file_ids = {f.id for f in r.files}
    assert set(r.metrics["files"].keys()) == file_ids
    for fid, m in r.metrics["files"].items():
        assert m["loc_total"] >= 0
        assert 0 < m["mi"] <= 100, fid
    # broken.py must still get a (degenerate but present) metrics entry
    assert "broken/broken.py" in r.metrics["files"]
    proj = r.metrics["project"]
    assert proj["files"] == len(r.files)
    assert proj["loc_code"] == sum(m["loc_code"] for m in r.metrics["files"].values())
    assert set(proj["languages"].keys()) == set(r.meta.languages.keys())


def test_pipeline_keep_sources_default_off():
    # Backward compatibility: analyze_project's new keep_sources kwarg
    # defaults to False, so pre-existing callers don't pay for/receive the
    # extra sources map.
    r = analyze_project(SAMPLE_FIXTURE)
    assert not hasattr(r, "_sources")


def test_pipeline_keep_sources_matches_disk_content():
    r = analyze_project(METRICS_FIXTURE, keep_sources=True)
    assert r._sources["pkg/calc.py"] == (METRICS_FIXTURE / "pkg" / "calc.py").read_text()
    assert set(r._sources.keys()) == {f.id for f in r.files}
