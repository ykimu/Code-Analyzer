"""Tests for the v1.1 network metrics (metrics/network.py).

These build tiny synthetic ``AnalysisResult`` objects directly (no parsing
needed) so every expected value is hand-computable and independently
re-derivable from the graph theory formulas documented in
``metrics/network.py``.
"""
from __future__ import annotations

import pytest

from codeanalyzer.core.model import AnalysisResult, Edge, FileInfo
from codeanalyzer.metrics.network import compute_network_metrics


def _file(fid: str) -> FileInfo:
    return FileInfo(id=fid, path=fid, language="python")


def _result(file_ids: list[str], edges: list[tuple[str, str]]) -> AnalysisResult:
    r = AnalysisResult()
    r.files = [_file(fid) for fid in file_ids]
    r.edges = [Edge(src=s, dst=d, kind="import") for s, d in edges]
    return r


# ---------------------------------------------------------------------------
# Directed path A -> B -> C
# ---------------------------------------------------------------------------

def test_path_graph_betweenness():
    # A -> B -> C: B lies on the only shortest path between A and C.
    # betweenness(B) = 1 / ((n-1)(n-2)) = 1 / (2*1) = 0.5
    r = _result(["A", "B", "C"], [("A", "B"), ("B", "C")])
    per_file, summary = compute_network_metrics(r)

    assert per_file["B"]["betweenness"] == 0.5
    assert per_file["A"]["betweenness"] == 0.0
    assert per_file["C"]["betweenness"] == 0.0

    assert summary["nodes"] == 3
    assert summary["edges"] == 2


def test_path_graph_closeness_incoming():
    # closeness(v) = harmonic mean of 1/d(u,v) over incoming directed paths.
    # A: nobody reaches A -> 0.0
    # B: A reaches B at distance 1 -> (1/1) / (n-1) = 1/2 = 0.5
    # C: B reaches C at distance 1, A reaches C at distance 2 ->
    #    (1/1 + 1/2) / 2 = 0.75
    r = _result(["A", "B", "C"], [("A", "B"), ("B", "C")])
    per_file, _ = compute_network_metrics(r)
    assert per_file["A"]["closeness"] == 0.0
    assert per_file["B"]["closeness"] == 0.5
    assert per_file["C"]["closeness"] == 0.75


def test_path_graph_degree_centrality():
    r = _result(["A", "B", "C"], [("A", "B"), ("B", "C")])
    per_file, _ = compute_network_metrics(r)
    # A: out=1,in=0 -> 1/2 ; B: out=1,in=1 -> 2/2=1.0 ; C: out=0,in=1 -> 1/2
    assert per_file["A"]["degree_centrality"] == 0.5
    assert per_file["B"]["degree_centrality"] == 1.0
    assert per_file["C"]["degree_centrality"] == 0.5


# ---------------------------------------------------------------------------
# Star graph: hub -> leaf1, hub -> leaf2, hub -> leaf3
# ---------------------------------------------------------------------------

def test_star_graph_hub_degree_centrality():
    r = _result(
        ["hub", "leaf1", "leaf2", "leaf3"],
        [("hub", "leaf1"), ("hub", "leaf2"), ("hub", "leaf3")],
    )
    per_file, summary = compute_network_metrics(r)
    # hub: out=3, in=0, n=4 -> 3/3 = 1.0
    assert per_file["hub"]["degree_centrality"] == 1.0
    # leaves: out=0, in=1, n=4 -> 1/3
    for leaf in ("leaf1", "leaf2", "leaf3"):
        assert per_file[leaf]["degree_centrality"] == pytest.approx(1 / 3, abs=1e-6)
    assert summary["nodes"] == 4
    assert summary["edges"] == 3
    assert summary["weakly_connected_components"] == 1
    assert summary["largest_component_size"] == 4


def test_star_graph_pagerank_hub_greater_than_leaf():
    r = _result(
        ["hub", "leaf1", "leaf2", "leaf3"],
        [("leaf1", "hub"), ("leaf2", "hub"), ("leaf3", "hub")],
    )
    per_file, _ = compute_network_metrics(r)
    total = sum(m["pagerank"] for m in per_file.values())
    assert total == pytest.approx(1.0, abs=1e-4)
    # hub is imported by all three leaves -> should rank higher than any leaf.
    assert per_file["hub"]["pagerank"] > per_file["leaf1"]["pagerank"]
    assert per_file["hub"]["pagerank"] > per_file["leaf2"]["pagerank"]
    assert per_file["hub"]["pagerank"] > per_file["leaf3"]["pagerank"]


# ---------------------------------------------------------------------------
# Isolated node
# ---------------------------------------------------------------------------

def test_isolated_node_zero_everywhere():
    # A -> B forms a small connected pair; C has no edges at all.
    r = _result(["A", "B", "C"], [("A", "B")])
    per_file, summary = compute_network_metrics(r)
    assert per_file["C"] == {
        "pagerank": 0.0, "betweenness": 0.0,
        "closeness": 0.0, "degree_centrality": 0.0,
    }
    # pagerank still sums to 1 across the whole file set, isolated or not.
    assert sum(m["pagerank"] for m in per_file.values()) == pytest.approx(1.0, abs=1e-4)
    assert summary["weakly_connected_components"] == 2
    assert summary["largest_component_size"] == 2


# ---------------------------------------------------------------------------
# Empty / degenerate graphs
# ---------------------------------------------------------------------------

def test_empty_graph_guards():
    r = _result([], [])
    per_file, summary = compute_network_metrics(r)
    assert per_file == {}
    assert summary == {
        "nodes": 0, "edges": 0, "density": 0.0, "avg_degree": 0.0,
        "weakly_connected_components": 0, "largest_component_size": 0,
    }


def test_single_node_no_edges_guards():
    r = _result(["A"], [])
    per_file, summary = compute_network_metrics(r)
    # No edges anywhere -> pagerank falls back to uniform 1/n = 1.0.
    assert per_file["A"] == {
        "pagerank": 1.0, "betweenness": 0.0,
        "closeness": 0.0, "degree_centrality": 0.0,
    }
    assert summary == {
        "nodes": 1, "edges": 0, "density": 0.0, "avg_degree": 0.0,
        "weakly_connected_components": 1, "largest_component_size": 1,
    }


def test_two_node_no_edges():
    r = _result(["A", "B"], [])
    per_file, summary = compute_network_metrics(r)
    for fid in ("A", "B"):
        assert per_file[fid]["degree_centrality"] == 0.0
        assert per_file[fid]["betweenness"] == 0.0
        assert per_file[fid]["closeness"] == 0.0
    assert summary["weakly_connected_components"] == 2
    assert summary["largest_component_size"] == 1


def test_self_loop_excluded():
    # A -> A must not create an edge, degree, or affect network stats.
    r = _result(["A", "B"], [("A", "A"), ("A", "B")])
    per_file, summary = compute_network_metrics(r)
    assert summary["edges"] == 1
    assert per_file["A"]["degree_centrality"] == 1.0  # out=1 in=0 -> 1/1
    assert per_file["B"]["degree_centrality"] == 1.0  # out=0 in=1 -> 1/1


def test_multi_edge_deduped():
    # Two Edge objects for the same (src, dst) pair must count as one edge.
    r = _result(
        ["A", "B"],
        [("A", "B"), ("A", "B")],
    )
    per_file, summary = compute_network_metrics(r)
    assert summary["edges"] == 1
    assert per_file["A"]["degree_centrality"] == 1.0


# ---------------------------------------------------------------------------
# External / unresolved targets excluded
# ---------------------------------------------------------------------------

def test_external_and_unresolved_targets_excluded():
    r = _result(["A", "B"], [("A", "B")])
    r.edges.append(Edge(src="A", dst="external:requests", kind="import"))
    r.edges.append(Edge(src="A", dst="unresolved:mystery", kind="import"))
    per_file, summary = compute_network_metrics(r)
    assert summary["nodes"] == 2
    assert summary["edges"] == 1
    assert set(per_file.keys()) == {"A", "B"}


def test_non_import_edges_excluded():
    r = _result(["A", "B"], [])
    r.edges.append(Edge(src="A::f", dst="B::g", kind="call"))
    per_file, summary = compute_network_metrics(r)
    assert summary["edges"] == 0
    assert per_file["A"]["degree_centrality"] == 0.0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_determinism_two_runs_equal():
    r = _result(
        ["A", "B", "C", "D"],
        [("A", "B"), ("B", "C"), ("C", "A"), ("D", "A")],
    )
    per_file_1, summary_1 = compute_network_metrics(r)
    per_file_2, summary_2 = compute_network_metrics(r)
    assert per_file_1 == per_file_2
    assert summary_1 == summary_2


def test_determinism_edge_insertion_order_independent():
    edges_a = [("A", "B"), ("B", "C"), ("C", "A"), ("D", "A")]
    edges_b = list(reversed(edges_a))
    r1 = _result(["A", "B", "C", "D"], edges_a)
    r2 = _result(["A", "B", "C", "D"], edges_b)
    per_file_1, summary_1 = compute_network_metrics(r1)
    per_file_2, summary_2 = compute_network_metrics(r2)
    assert per_file_1 == per_file_2
    assert summary_1 == summary_2


# ---------------------------------------------------------------------------
# Summary counts: two disconnected pairs
# ---------------------------------------------------------------------------

def test_summary_two_disconnected_pairs():
    r = _result(["A", "B", "C", "D"], [("A", "B"), ("C", "D")])
    per_file, summary = compute_network_metrics(r)
    assert summary["nodes"] == 4
    assert summary["edges"] == 2
    assert summary["density"] == pytest.approx(2 / (4 * 3), abs=1e-6)
    assert summary["avg_degree"] == pytest.approx(2 / 4, abs=1e-6)
    assert summary["weakly_connected_components"] == 2
    assert summary["largest_component_size"] == 2


# ---------------------------------------------------------------------------
# Cycle graph (strongly connected): sanity-check betweenness/pagerank/closeness
# ---------------------------------------------------------------------------

def test_three_cycle_symmetric_metrics():
    # A -> B -> C -> A: fully symmetric, every node should have identical
    # pagerank/betweenness/closeness/degree_centrality.
    r = _result(["A", "B", "C"], [("A", "B"), ("B", "C"), ("C", "A")])
    per_file, summary = compute_network_metrics(r)
    values = list(per_file.values())
    assert all(v == values[0] for v in values)
    assert sum(m["pagerank"] for m in per_file.values()) == pytest.approx(1.0, abs=1e-4)
    assert summary["weakly_connected_components"] == 1
    assert summary["largest_component_size"] == 3
