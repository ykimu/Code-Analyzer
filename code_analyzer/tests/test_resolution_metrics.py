"""Tests for metrics["resolution"] (v1.6 contract, core/model.py "v1.6
additions" / metrics/engine.py compute_resolution_metrics).

Fixture: ``tests/fixtures/resolution_sample`` (new, purpose-built for this
file) -- a tiny mixed python/C project whose CALL/IMPORT edge outcomes were
worked out by hand from the resolver rules (resolvers/symbols.py,
resolvers/modules.py) so every number here is independently re-derivable:

Python side (respkg/, named to avoid colliding with the "pkg" package name
already used by tests/fixtures/metrics_sample during pytest test-file
collection):
  - dup1.py::shared / dup2.py::shared -- two same-named functions.
  - caller.py::use() calls ``shared()`` with no import binding -> project-
    wide name lookup finds both candidates -> 2 ambiguous/inferred CALL
    edges.
  - main.py::run() calls ``shared()`` via ``from .dup1 import shared`` ->
    single candidate -> 1 certain CALL edge.
  - test_caller.py::test_it() (test file: name starts with "test_") makes
    the identical import-bound certain call -> this edge must be excluded
    from resolution.calls.non_test.
  - unresolved_caller.py::go() calls an undefined name -> 1 unresolved CALL
    edge.
  So python CALL edges: total=5, certain=2, inferred=2, unresolved=1,
  ambiguous=2 (of which 1 certain + 1 unresolved are "non_test"-excluded...
  actually only the test_caller.py certain edge is test-sourced).

C side (c/): main.c calls two functions that are only declared (never
  defined) in util.h/feature.h -- util_fn() and printf() -- so both CALL
  edges resolve to "unresolved" (no FUNCTION symbol exists for either).
  main.c's IMPORT edges: util.h (certain, unconditional), feature.h
  (certain, wrapped in ``#ifdef FEATURE_X`` -> Edge.conditional=True),
  <stdio.h> (external).

Combined (python + c) CALL edges: total=7, certain=2, inferred=2,
unresolved=3, ambiguous=2.
Combined IMPORT edges: total=5, certain=4 (util.h, feature.h, and the two
  python "from .dup1 import shared" edges), external=1, unresolved=0,
  conditional=1 (feature.h).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from codeanalyzer.core.pipeline import analyze_project
from codeanalyzer.metrics.engine import compute_metrics

RESOLUTION_FIXTURE = Path(__file__).parent / "fixtures" / "resolution_sample"

_CALLS_KEYS = {
    "total", "certain", "inferred", "unresolved", "ambiguous",
    "certain_pct", "ambiguous_pct", "non_test",
}
_COUNTS_KEYS = {
    "total", "certain", "inferred", "unresolved", "ambiguous",
    "certain_pct", "ambiguous_pct",
}
_IMPORTS_KEYS = {"total", "certain", "unresolved", "external", "conditional"}


@pytest.fixture(scope="module")
def resolution_result():
    r = analyze_project(RESOLUTION_FIXTURE, keep_sources=True)
    compute_metrics(r, r._sources)
    return r


# ---------------------------------------------------------------------------
# exact key-set contract
# ---------------------------------------------------------------------------

def test_resolution_top_level_keys(resolution_result):
    res = resolution_result.metrics["resolution"]
    assert set(res.keys()) == {"calls", "imports", "by_language", "note"}
    assert set(res["calls"].keys()) == _CALLS_KEYS
    assert set(res["calls"]["non_test"].keys()) == _COUNTS_KEYS
    assert set(res["imports"].keys()) == _IMPORTS_KEYS
    assert isinstance(res["note"], str) and res["note"]
    for lang, entry in res["by_language"].items():
        assert set(entry.keys()) == {
            "calls_total", "calls_certain_pct", "calls_ambiguous_pct"}


def test_resolution_definitions_present(resolution_result):
    defs = resolution_result.metrics["definitions"]
    assert "resolution_calls" in defs
    assert "resolution_calls_non_test" in defs
    assert "resolution_imports" in defs
    assert "resolution_by_language" in defs
    assert all(isinstance(v, str) for v in defs.values())


# ---------------------------------------------------------------------------
# calls family: hand-computed counts (see module docstring)
# ---------------------------------------------------------------------------

def test_resolution_calls_counts(resolution_result):
    calls = resolution_result.metrics["resolution"]["calls"]
    assert calls["total"] == 7
    assert calls["certain"] == 2
    assert calls["inferred"] == 2
    assert calls["unresolved"] == 3
    assert calls["ambiguous"] == 2
    assert calls["certain_pct"] == pytest.approx(28.6)
    assert calls["ambiguous_pct"] == pytest.approx(28.6)


def test_resolution_calls_non_test_excludes_test_file_src(resolution_result):
    # test_caller.py::test_it -> dup1.py::shared is a certain edge whose SRC
    # file is a test file (name starts with "test_") -> excluded.
    non_test = resolution_result.metrics["resolution"]["calls"]["non_test"]
    assert non_test["total"] == 6
    assert non_test["certain"] == 1
    assert non_test["inferred"] == 2
    assert non_test["unresolved"] == 3
    assert non_test["ambiguous"] == 2
    assert non_test["certain_pct"] == pytest.approx(16.7)
    assert non_test["ambiguous_pct"] == pytest.approx(33.3)


# ---------------------------------------------------------------------------
# imports: certain/unresolved/external/conditional
# ---------------------------------------------------------------------------

def test_resolution_imports_counts(resolution_result):
    imports = resolution_result.metrics["resolution"]["imports"]
    assert imports["total"] == 5
    assert imports["certain"] == 4
    assert imports["unresolved"] == 0
    assert imports["external"] == 1
    assert imports["conditional"] == 1


def test_resolution_imports_conditional_matches_edge_flag(resolution_result):
    import_edges = [e for e in resolution_result.edges if e.kind == "import"]
    expected = sum(1 for e in import_edges if e.conditional)
    assert resolution_result.metrics["resolution"]["imports"]["conditional"] == expected
    assert expected == 1
    cond_edges = [e for e in import_edges if e.conditional]
    assert cond_edges[0].dst == "c/feature.h"


# ---------------------------------------------------------------------------
# by_language: present for every language in the project, src-file language
# ---------------------------------------------------------------------------

def test_resolution_by_language(resolution_result):
    by_lang = resolution_result.metrics["resolution"]["by_language"]
    assert set(by_lang.keys()) == {"python", "c"}

    assert by_lang["python"]["calls_total"] == 5
    assert by_lang["python"]["calls_certain_pct"] == pytest.approx(40.0)
    assert by_lang["python"]["calls_ambiguous_pct"] == pytest.approx(40.0)

    assert by_lang["c"]["calls_total"] == 2
    assert by_lang["c"]["calls_certain_pct"] == pytest.approx(0.0)
    assert by_lang["c"]["calls_ambiguous_pct"] == pytest.approx(0.0)


def test_resolution_by_language_deterministic_order(resolution_result):
    # languages sorted alphabetically -> deterministic across runs.
    assert list(resolution_result.metrics["resolution"]["by_language"].keys()) == ["c", "python"]


# ---------------------------------------------------------------------------
# pct math sanity: certain_pct/ambiguous_pct always in [0, 100], 1dp
# ---------------------------------------------------------------------------

def test_resolution_pct_bounds_and_rounding(resolution_result):
    res = resolution_result.metrics["resolution"]
    for counts in (res["calls"], res["calls"]["non_test"],
                   *res["by_language"].values()):
        cert = counts.get("certain_pct")
        amb = counts.get("ambiguous_pct")
        if cert is not None:
            assert 0.0 <= cert <= 100.0
            assert round(cert, 1) == cert
        if amb is not None:
            assert 0.0 <= amb <= 100.0
            assert round(amb, 1) == amb


def test_resolution_pct_zero_when_no_edges():
    # A project with no calls-family edges at all must report 0.0 pct
    # values rather than raising ZeroDivisionError.
    empty_fixture = Path(__file__).parent / "fixtures" / "metrics_sample"
    r = analyze_project(empty_fixture, keep_sources=True)
    compute_metrics(r, r._sources)
    res = r.metrics["resolution"]
    # metrics_sample has no INHERIT/TYPE_REF edges and does have CALL edges,
    # so instead directly exercise the zero-total branch through the pct
    # helper contract: an empty edge list must yield 0.0, not a crash.
    from codeanalyzer.metrics.engine import _confidence_counts
    counts = _confidence_counts([])
    assert counts == {
        "total": 0, "certain": 0, "inferred": 0, "unresolved": 0,
        "ambiguous": 0, "certain_pct": 0.0, "ambiguous_pct": 0.0,
    }
    assert res  # sanity: resolution key still present for this fixture
