from codeanalyzer.core.graph import (
    build_graphs,
    reverse_bfs,
    tarjan_scc,
)
from codeanalyzer.core.model import Edge, EdgeKind


def _imp(src, dst):
    return Edge(src=src, dst=dst, kind=EdgeKind.IMPORT.value)


def _call(src, dst):
    return Edge(src=src, dst=dst, kind=EdgeKind.CALL.value)


def test_build_graphs_splits_kinds():
    edges = [_imp("a.py", "b.py"), _call("a.py::f", "b.py::g")]
    fg, sg = build_graphs(edges)
    assert "b.py" in fg.forward["a.py"]
    assert "b.py.py" not in fg.nodes
    assert "b.py::g" in sg.forward["a.py::f"]
    assert "a.py::f" not in fg.nodes


def test_tarjan_detects_cycle():
    edges = [
        _imp("a", "b"), _imp("b", "c"), _imp("c", "a"),  # 3-cycle
        _imp("c", "d"),                                   # d is a sink
        _imp("d", "external:x"),
    ]
    fg, _ = build_graphs(edges)
    internal = {"a", "b", "c", "d"}
    cycles = tarjan_scc(fg, only_nodes=internal)
    assert cycles == [["a", "b", "c"]]


def test_tarjan_no_false_cycle():
    edges = [_imp("a", "b"), _imp("b", "c")]
    fg, _ = build_graphs(edges)
    assert tarjan_scc(fg, only_nodes={"a", "b", "c"}) == []


def test_tarjan_two_disjoint_cycles():
    edges = [
        _imp("a", "b"), _imp("b", "a"),
        _imp("x", "y"), _imp("y", "x"),
    ]
    fg, _ = build_graphs(edges)
    cycles = tarjan_scc(fg)
    assert ["a", "b"] in cycles
    assert ["x", "y"] in cycles
    assert cycles == sorted(cycles)


def test_reverse_bfs_hops():
    # d imports c imports b imports a  (edges point importer -> imported)
    edges = [_imp("b", "a"), _imp("c", "b"), _imp("d", "c")]
    fg, _ = build_graphs(edges)
    # who depends (transitively) on a?
    dist = reverse_bfs(fg, ["a"], kinds={EdgeKind.IMPORT.value}, max_hops=10)
    assert dist["a"] == 0
    assert dist["b"] == 1
    assert dist["c"] == 2
    assert dist["d"] == 3


def test_reverse_bfs_respects_max_hops():
    edges = [_imp("b", "a"), _imp("c", "b"), _imp("d", "c")]
    fg, _ = build_graphs(edges)
    dist = reverse_bfs(fg, ["a"], max_hops=1)
    assert "b" in dist
    assert "c" not in dist
