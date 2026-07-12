"""Tests for the change-impact analyzer (SPEC F6).

Fixtures live in ``tests/fixtures/impact_project``:

* call chain ``a() -> b() -> c()`` spread across ``mod_a/mod_b/mod_c.py``;
* ``importer.py`` -- imports ``mod_c`` but never calls into it (import-only
  dependent, propagated at the module level);
* ``dup1.py`` / ``dup2.py`` both define ``shared()`` and ``ambcaller.py`` calls
  ``shared()`` unqualified -> ambiguous same-name resolution;
* ``flow.py`` -- ``passthrough(p)`` returns its parameter (return flow) and
  ``flow_caller()`` passes a local into it (argument flow);
* ``app.ts`` -- a TypeScript call chain ``tsCaller() -> tsFunc()``;
* ``mod_a.py::a`` also calls an undefined ``missing_thing()`` -> unresolved
  boundary reachable from the chain.
"""
from pathlib import Path

import pytest

from codeanalyzer.core.pipeline import analyze_project
from codeanalyzer.impact.analyzer import analyze_impact, format_impact_text

FIXTURE = Path(__file__).parent / "fixtures" / "impact_project"

# ImpactResult top-level shape documented at the bottom of core/model.py
RESULT_KEYS = {
    "query", "origin", "affected_files", "sliced_vars",
    "unresolved_boundaries", "stats",
}


@pytest.fixture(scope="module")
def result():
    return analyze_project(FIXTURE)


def _sym_map(impact):
    out = {}
    for af in impact["affected_files"]:
        for s in af["symbols"]:
            out[s["symbol_id"]] = dict(s, file_id=af["file_id"])
    return out


# ---------------------------------------------------------------------------
# origin detection
# ---------------------------------------------------------------------------
def test_origin_enclosing_function(result):
    imp = analyze_impact(result, "mod_c.py", 2)
    assert imp["origin"]["symbol_id"] == "mod_c.py::c"
    assert imp["origin"]["kind"] == "function"


def test_origin_module_level_line(result):
    # importer.py line 1 is a module-level `import` statement (no enclosing def)
    imp = analyze_impact(result, "importer.py", 1)
    assert imp["origin"]["kind"] == "module"
    assert imp["origin"]["symbol_id"] == "importer.py::<module>"
    assert "error" not in imp


def test_blank_line_error(result):
    # mod_c.py line 4 is a blank line at module scope
    imp = analyze_impact(result, "mod_c.py", 4)
    assert "error" in imp
    assert "blank" in imp["error"].lower() or "comment" in imp["error"].lower()


def test_unknown_file_error(result):
    imp = analyze_impact(result, "does_not_exist.py", 1)
    assert "error" in imp


def test_file_path_normalization(result):
    # absolute path spelling should resolve to the same origin as the id
    abs_path = str(FIXTURE / "mod_c.py")
    imp = analyze_impact(result, abs_path, 2)
    assert imp["origin"]["symbol_id"] == "mod_c.py::c"


# ---------------------------------------------------------------------------
# result shape
# ---------------------------------------------------------------------------
def test_result_key_set(result):
    imp = analyze_impact(result, "mod_c.py", 1)
    assert set(imp.keys()) == RESULT_KEYS
    assert set(imp["query"].keys()) == {"file", "start_line", "end_line", "depth"}
    assert set(imp["origin"].keys()) == {"symbol_id", "symbol_name", "kind"}
    for af in imp["affected_files"]:
        assert set(af.keys()) == {"file_id", "symbols"}
        for s in af["symbols"]:
            assert set(s.keys()) == {
                "symbol_id", "lines", "confidence", "via", "hops"}
            assert s["confidence"] in ("certain", "inferred")
            assert s["via"] in ("call", "import", "dataflow")
    assert set(imp["stats"].keys()) == {"files", "symbols", "max_hops_reached"}


# ---------------------------------------------------------------------------
# backward impact: call chain + hop counts
# ---------------------------------------------------------------------------
def test_backward_call_chain_hops(result):
    imp = analyze_impact(result, "mod_c.py", 1)
    syms = _sym_map(imp)
    # origin at hop 0
    assert syms["mod_c.py::c"]["hops"] == 0
    # b calls c -> hop 1; a calls b -> hop 2
    assert syms["mod_b.py::b"]["hops"] == 1
    assert syms["mod_b.py::b"]["via"] == "call"
    assert syms["mod_a.py::a"]["hops"] == 2
    assert syms["mod_a.py::a"]["via"] == "call"
    # call-site reference lines recorded in the dependent's file
    assert 5 in syms["mod_b.py::b"]["lines"]   # `return c(n)`
    assert 6 in syms["mod_a.py::a"]["lines"]   # `return b(5)`


def test_import_only_dependent(result):
    imp = analyze_impact(result, "mod_c.py", 1)
    syms = _sym_map(imp)
    # importer.py imports mod_c but never calls c -> module-level, via import
    assert "importer.py::<module>" in syms
    assert syms["importer.py::<module>"]["via"] == "import"
    assert syms["importer.py::<module>"]["hops"] == 1


def test_transitive_import_hops(result):
    imp = analyze_impact(result, "mod_c.py", 1)
    syms = _sym_map(imp)
    # mod_a imports mod_b imports mod_c -> mod_a module at import hop 2
    assert syms["mod_a.py::<module>"]["via"] == "import"
    assert syms["mod_a.py::<module>"]["hops"] == 2


def test_affected_files_sorted_by_min_hops(result):
    imp = analyze_impact(result, "mod_c.py", 1)
    order = [af["file_id"] for af in imp["affected_files"]]
    # origin file first (min hop 0), then hop-1 files, then hop-2
    assert order[0] == "mod_c.py"
    # symbols within a file sorted by (hops, symbol_id)
    for af in imp["affected_files"]:
        keyed = [(s["hops"], s["symbol_id"]) for s in af["symbols"]]
        assert keyed == sorted(keyed)


# ---------------------------------------------------------------------------
# confidence downgrade on ambiguity
# ---------------------------------------------------------------------------
def test_ambiguous_downgrades_confidence(result):
    imp = analyze_impact(result, "dup1.py", 1)
    syms = _sym_map(imp)
    # caller_amb calls shared() ambiguously (dup1/dup2) -> inferred, not certain
    assert "ambcaller.py::caller_amb" in syms
    assert syms["ambcaller.py::caller_amb"]["confidence"] == "inferred"


def test_ambiguous_boundary_recorded(result):
    imp = analyze_impact(result, "dup1.py", 1)
    bnds = imp["unresolved_boundaries"]
    assert any(b["symbol_id"] == "ambcaller.py::caller_amb"
               and "ambiguous" in b["reason"] and "2" in b["reason"]
               for b in bnds)


# ---------------------------------------------------------------------------
# forward data-flow slice
# ---------------------------------------------------------------------------
def test_sliced_vars_detected(result):
    # querying the def of `result` (line 2 of mod_c.py) slices `result`
    imp = analyze_impact(result, "mod_c.py", 2)
    assert imp["sliced_vars"] == ["result"]
    syms = _sym_map(imp)
    # its use is at line 3 (`return result`), marked via dataflow on origin
    origin = syms["mod_c.py::c"]
    assert origin["via"] == "dataflow"
    assert 2 in origin["lines"] and 3 in origin["lines"]


def test_slice_ignores_out_of_range_vars(result):
    # querying line 1 (param `val`) slices only `val`, not `result`
    imp = analyze_impact(result, "mod_c.py", 1)
    assert imp["sliced_vars"] == ["val"]


def test_return_flow_marks_caller_call_site(result):
    # passthrough(p) returns p; querying its param line propagates to callers
    imp = analyze_impact(result, "flow.py", 1)
    assert imp["sliced_vars"] == ["p"]
    syms = _sym_map(imp)
    assert "flow.py::flow_caller" in syms
    # flow_caller's call site (`return passthrough(z)`, line 7) is affected
    assert 7 in syms["flow.py::flow_caller"]["lines"]


def test_param_flow_marks_callee_uses(result):
    # flow_caller passes local z into passthrough; slice flows into the callee
    imp = analyze_impact(result, "flow.py", 6)
    assert imp["sliced_vars"] == ["z"]
    syms = _sym_map(imp)
    assert "flow.py::passthrough" in syms
    callee = syms["flow.py::passthrough"]
    assert callee["via"] == "dataflow"
    assert callee["confidence"] == "inferred"
    assert callee["hops"] == 1
    # parameter def line (1) and its use line (2) are marked
    assert 1 in callee["lines"] and 2 in callee["lines"]


# ---------------------------------------------------------------------------
# unresolved boundaries
# ---------------------------------------------------------------------------
def test_unresolved_boundary_populated(result):
    # mod_a.py::a calls the undefined `missing_thing()` and is on the chain
    imp = analyze_impact(result, "mod_c.py", 1)
    bnds = imp["unresolved_boundaries"]
    assert any(b["symbol_id"] == "mod_a.py::a"
               and "missing_thing" in b["reason"]
               and b["line"] == 5 for b in bnds)


def test_boundaries_deduplicated_and_sorted(result):
    imp = analyze_impact(result, "mod_c.py", 1)
    bnds = imp["unresolved_boundaries"]
    keys = [(b["symbol_id"], b["line"], b["reason"]) for b in bnds]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# cross-language (TypeScript)
# ---------------------------------------------------------------------------
def test_typescript_backward_impact(result):
    imp = analyze_impact(result, "app.ts", 1)
    assert imp["origin"]["symbol_id"] == "app.ts::tsFunc"
    syms = _sym_map(imp)
    assert "app.ts::tsCaller" in syms
    assert syms["app.ts::tsCaller"]["via"] == "call"
    assert syms["app.ts::tsCaller"]["hops"] == 1


# ---------------------------------------------------------------------------
# depth control + truncation flag
# ---------------------------------------------------------------------------
def test_max_depth_truncation(result):
    full = analyze_impact(result, "mod_c.py", 1, max_depth=10)
    assert full["stats"]["max_hops_reached"] is False
    shallow = analyze_impact(result, "mod_c.py", 1, max_depth=1)
    syms = _sym_map(shallow)
    # b is at hop 1 (included), a is at hop 2 (cut off)
    assert "mod_b.py::b" in syms
    assert "mod_a.py::a" not in syms
    assert shallow["stats"]["max_hops_reached"] is True


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------
def test_determinism(result):
    a = analyze_impact(result, "mod_c.py", 1)
    b = analyze_impact(result, "mod_c.py", 1)
    assert a == b
    # a fresh analysis run yields an identical impact result too
    fresh = analyze_project(FIXTURE)
    c = analyze_impact(fresh, "mod_c.py", 1)
    assert a == c


def test_stats_counts(result):
    imp = analyze_impact(result, "mod_c.py", 1)
    n_files = len(imp["affected_files"])
    n_syms = sum(len(af["symbols"]) for af in imp["affected_files"])
    assert imp["stats"]["files"] == n_files
    assert imp["stats"]["symbols"] == n_syms


# ---------------------------------------------------------------------------
# text rendering
# ---------------------------------------------------------------------------
def test_format_impact_text(result):
    imp = analyze_impact(result, "mod_c.py", 1)
    txt = format_impact_text(imp)
    assert "mod_c.py::c" in txt
    assert "mod_a.py::a" in txt
    assert "missing_thing" in txt          # boundary warning shown
    assert isinstance(txt, str) and txt


def test_format_impact_text_error():
    txt = format_impact_text({"error": "boom"})
    assert "boom" in txt
