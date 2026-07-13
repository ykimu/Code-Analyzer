"""Tests for C/C++ preprocessor-conditional #include detection (v1.6).

``Edge.conditional`` (core/model.py) is True for a C/C++ IMPORT edge whose
``#include`` directive sits inside a preprocessor conditional region
(#if / #ifdef / #ifndef / #elif / #else, nesting-aware), EXCEPT the common
whole-file include-guard wrapper (a top-level ``#ifndef GUARD`` / ``#define
GUARD`` spanning the entire file), whose direct content is NOT considered
conditional.

Fixture: ``tests/fixtures/cond_includes`` -- five small C/C++ files, each
purpose-built for one detection case:

- ``plain.c``: two top-level includes -> both unconditional.
- ``ifdef.c``: one top-level include, one inside ``#ifdef`` (then-branch),
  one inside the matching ``#else`` branch.
- ``nested.c``: one top-level include, one nested two ``#ifdef`` levels deep.
- ``guard.h``: whole-file ``#ifndef GUARD_H``/``#define GUARD_H`` wrapper;
  one include directly inside the guard (unconditional -- guard-exempt) and
  one inside a nested ``#ifdef`` *within* the guard (conditional).
- ``platform.cpp``: ``#if defined(_WIN32) ... #else ... #endif``.
"""
from __future__ import annotations

from pathlib import Path

from codeanalyzer.core.pipeline import analyze_project

FIXTURE = Path(__file__).parent / "fixtures" / "cond_includes"
SAMPLE_FIXTURE = Path(__file__).parent / "fixtures" / "sample_project"


def _import_edges(result, src: str) -> list:
    return [e for e in result.edges if e.kind == "import" and e.src == src]


def _edge(result, src: str, dst_suffix: str):
    for e in _import_edges(result, src):
        if e.dst.endswith(dst_suffix):
            return e
    raise AssertionError(f"no import edge {src} -> *{dst_suffix} found")


# ---------------------------------------------------------------------------
# Case: plain top-level include -> conditional False
# ---------------------------------------------------------------------------
def test_plain_top_level_include_not_conditional():
    r = analyze_project(FIXTURE)
    stdio = _edge(r, "plain.c", "stdio.h")
    guard = _edge(r, "plain.c", "guard.h")
    assert stdio.conditional is False
    assert guard.conditional is False


# ---------------------------------------------------------------------------
# Case: include inside #ifdef FEATURE (then-branch) -> True
# Case: include inside the matching #else branch -> True
# ---------------------------------------------------------------------------
def test_ifdef_then_branch_conditional():
    r = analyze_project(FIXTURE)
    guard = _edge(r, "ifdef.c", "guard.h")   # inside #ifdef FEATURE_A
    assert guard.conditional is True


def test_else_branch_conditional():
    r = analyze_project(FIXTURE)
    stdlib_edge = _edge(r, "ifdef.c", "stdlib.h")  # inside #else
    assert stdlib_edge.conditional is True


def test_top_level_include_alongside_ifdef_stays_unconditional():
    r = analyze_project(FIXTURE)
    stdio = _edge(r, "ifdef.c", "stdio.h")
    assert stdio.conditional is False


# ---------------------------------------------------------------------------
# Case: nested #ifdef within #ifdef -> True (nesting-aware)
# ---------------------------------------------------------------------------
def test_nested_ifdef_conditional():
    r = analyze_project(FIXTURE)
    math_edge = _edge(r, "nested.c", "math.h")
    assert math_edge.conditional is True
    stdio = _edge(r, "nested.c", "stdio.h")
    assert stdio.conditional is False


# ---------------------------------------------------------------------------
# Case: whole-file include-guard wrapper -> includes directly inside are
# NOT conditional, but a further nested conditional inside the guard still
# marks its include True.
# ---------------------------------------------------------------------------
def test_include_guard_wrapped_header_not_conditional():
    r = analyze_project(FIXTURE)
    stddef = _edge(r, "guard.h", "stddef.h")
    assert stddef.conditional is False


def test_nested_conditional_inside_guard_still_conditional():
    r = analyze_project(FIXTURE)
    time_edge = _edge(r, "guard.h", "time.h")
    assert time_edge.conditional is True


# ---------------------------------------------------------------------------
# Case: .cpp with #if defined(_WIN32) ... #else ... #endif -> both True
# ---------------------------------------------------------------------------
def test_cpp_if_defined_conditional():
    r = analyze_project(FIXTURE)
    windows_edge = _edge(r, "platform.cpp", "windows.h")
    unistd_edge = _edge(r, "platform.cpp", "unistd.h")
    assert windows_edge.conditional is True
    assert unistd_edge.conditional is True


# ---------------------------------------------------------------------------
# The flag must survive JSON serialization (analysis.json contract).
# ---------------------------------------------------------------------------
def test_conditional_flag_survives_analysis_json():
    r = analyze_project(FIXTURE)
    data = r.to_dict()
    edges = data["edges"]
    assert edges, "expected import edges in analysis.json"

    def find(src, dst_suffix):
        for e in edges:
            if e["src"] == src and e["kind"] == "import" \
                    and e["dst"].endswith(dst_suffix):
                return e
        raise AssertionError(f"no edge {src} -> *{dst_suffix} in analysis.json")

    assert find("plain.c", "stdio.h")["conditional"] is False
    assert find("ifdef.c", "guard.h")["conditional"] is True
    assert find("ifdef.c", "stdlib.h")["conditional"] is True
    assert find("nested.c", "math.h")["conditional"] is True
    assert find("guard.h", "stddef.h")["conditional"] is False
    assert find("guard.h", "time.h")["conditional"] is True
    assert find("platform.cpp", "windows.h")["conditional"] is True
    assert find("platform.cpp", "unistd.h")["conditional"] is True


# ---------------------------------------------------------------------------
# Cross-project serialization check: every edge dict (not just C/C++
# imports) carries the "conditional" key, defaulting to False for the
# existing sample_project fixture (which has no preprocessor conditionals).
# ---------------------------------------------------------------------------
def test_sample_project_edges_have_conditional_key_false():
    r = analyze_project(SAMPLE_FIXTURE)
    data = r.to_dict()
    edges = data["edges"]
    assert edges, "expected edges in sample_project analysis.json"
    for e in edges:
        assert "conditional" in e
        assert e["conditional"] is False

    c_import_edges = [e for e in edges
                       if e["kind"] == "import"
                       and (e["src"].endswith(".c") or e["src"].endswith(".h")
                            or e["src"].endswith(".cpp"))]
    assert c_import_edges, "expected at least one C/C++ import edge"
    for e in c_import_edges:
        assert e["conditional"] is False
