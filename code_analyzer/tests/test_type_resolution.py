"""v1.6 Python type-aware call-resolution narrowing (contract items a-d).

Exercises the type_project fixture: annotations, constructor assignments,
self./ClassName. receivers, return annotations, string/Optional forms, an
unknown receiver (regression guard), a shadowing case that must NOT narrow,
and a whole-project never-widen invariant.
"""
import os
from pathlib import Path

import pytest

from codeanalyzer.core.pipeline import analyze_project

FIXTURE = Path(__file__).parent / "fixtures" / "type_project"
_NARROW_ENV = "CODE_ANALYZER_NO_TYPE_NARROWING"


def _analyze(narrow: bool):
    old = os.environ.get(_NARROW_ENV)
    if narrow:
        os.environ.pop(_NARROW_ENV, None)
    else:
        os.environ[_NARROW_ENV] = "1"
    try:
        return analyze_project(FIXTURE, respect_gitignore=False)
    finally:
        if old is None:
            os.environ.pop(_NARROW_ENV, None)
        else:
            os.environ[_NARROW_ENV] = old


@pytest.fixture(scope="module")
def edges():
    return _analyze(narrow=True).edges


def _save_calls(edges, caller):
    """CALL edges to a ``save`` method emitted from ``caller`` function."""
    return [e for e in edges
            if e.kind == "call" and e.dst.endswith("::save")
            and e.src.endswith("::" + caller)]


def _dsts(edges, caller):
    return {e.dst for e in _save_calls(edges, caller)}


def test_annotation_param(edges):
    # (a) def f(x: Alpha): x.save() -> only Alpha.save, certain
    calls = _save_calls(edges, "by_annotation")
    assert {e.dst for e in calls} == {"alpha.py::Alpha::save"}
    assert len(calls) == 1
    assert calls[0].confidence == "certain" and calls[0].ambiguous is False


def test_string_annotation(edges):
    # (a) x: "Alpha" string form
    calls = _save_calls(edges, "by_string")
    assert {e.dst for e in calls} == {"alpha.py::Alpha::save"}
    assert calls[0].confidence == "certain"


def test_optional_annotation(edges):
    # (a) x: Optional[Alpha]
    calls = _save_calls(edges, "by_optional")
    assert {e.dst for e in calls} == {"alpha.py::Alpha::save"}
    assert calls[0].confidence == "certain"


def test_constructor_assignment(edges):
    # (b) y = Beta(); y.save() -> Beta.save certain
    calls = _save_calls(edges, "by_constructor")
    assert {e.dst for e in calls} == {"beta.py::Beta::save"}
    assert calls[0].confidence == "certain"


def test_self_inheritance(edges):
    # (c) self.save() with Child(Base), save on Base -> Base.save certain
    calls = _save_calls(edges, "run")
    assert {e.dst for e in calls} == {"base_mod.py::Base::save"}
    assert calls[0].confidence == "certain"


def test_direct_class(edges):
    # (d) Alpha.save() direct
    calls = _save_calls(edges, "by_direct")
    assert {e.dst for e in calls} == {"alpha.py::Alpha::save"}
    assert calls[0].confidence == "certain"


def test_return_annotation(edges):
    # (a) make() -> Alpha; make().save() -> Alpha.save certain
    calls = _save_calls(edges, "by_return")
    assert {e.dst for e in calls} == {"alpha.py::Alpha::save"}
    assert calls[0].confidence == "certain"


def test_unknown_receiver_unchanged(edges):
    # regression guard: z.save() stays ambiguous across all name candidates
    calls = _save_calls(edges, "by_unknown")
    assert len(calls) >= 2
    assert all(e.ambiguous and e.confidence == "inferred" for e in calls)
    # unchanged vs. narrowing-disabled run
    plain = _analyze(narrow=False).edges
    assert _dsts(edges, "by_unknown") == _dsts(plain, "by_unknown")


def test_shadowing_no_narrowing(edges):
    # x = Alpha(); x = helper(); x.save() -> reassigned to unknown -> no narrow
    calls = _save_calls(edges, "by_shadow")
    assert len(calls) >= 2
    assert all(e.ambiguous for e in calls)
    plain = _analyze(narrow=False).edges
    assert _dsts(edges, "by_shadow") == _dsts(plain, "by_shadow")


def test_never_widen_whole_project():
    """Every (src, kind, line) dst-set with narrowing is a subset of the
    dst-set produced with narrowing disabled."""
    narrowed = _analyze(narrow=True).edges
    plain = _analyze(narrow=False).edges

    def grouped(edges):
        g: dict[tuple, set[str]] = {}
        for e in edges:
            g.setdefault((e.src, e.kind, e.line), set()).add(e.dst)
        return g

    gn, gp = grouped(narrowed), grouped(plain)
    for key, dsts in gn.items():
        assert key in gp, f"narrowed produced a new group {key}"
        assert dsts <= gp[key], f"widened at {key}: {dsts} !<= {gp[key]}"


def test_narrowing_actually_helps():
    """Sanity: narrowing raises certain-count and lowers ambiguous-count."""
    narrowed = _analyze(narrow=True).edges
    plain = _analyze(narrow=False).edges

    def stats(edges):
        calls = [e for e in edges if e.kind in ("call", "inherit", "type_ref")]
        certain = sum(1 for e in calls if e.confidence == "certain")
        ambiguous = sum(1 for e in calls if e.ambiguous)
        return certain, ambiguous

    c_n, a_n = stats(narrowed)
    c_p, a_p = stats(plain)
    assert c_n > c_p
    assert a_n < a_p
