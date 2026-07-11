"""Graph-theoretic network metrics over the internal file-level IMPORT graph.

``compute_network_metrics(result)`` is pure and side-effect free: it reads
``result.files``/``result.edges`` and returns ``(per_file, network_summary)``
-- it does not mutate ``result``.  The metrics engine (``metrics/engine.py``)
is responsible for merging the result into ``metrics["files"]`` and setting
``metrics["network"]``.

Graph construction (shared by every metric below):

* Nodes are every internal file id (``result.files``), including files with
  no import edges at all ("isolated" nodes) -- every internal file gets an
  entry in ``per_file``.
* Edges are IMPORT edges whose ``dst`` resolves to another internal file
  (``external:...`` and ``unresolved:...`` targets are excluded, mirroring
  ``metrics/engine.py``'s fan_in/fan_out treatment).
* Multi-edges are deduplicated to a single (src, dst) pair (matches the
  fan_in/fan_out "dedup per (src, dst)" rule in ``metrics/engine.py``).
* Self-loops (src == dst) are excluded.

All floating point outputs are rounded to 6 decimal places. Node iteration
order is always the sorted file id order, and adjacency is always walked in
sorted order, so results are fully deterministic and independent of input
edge order.

Metric definitions:

* **pagerank** -- classic PageRank (damping = 0.85) via power iteration
  (stop when the L1 delta between iterations drops below 1e-10, or after
  200 iterations). Dangling nodes (no outgoing edges) redistribute their
  rank uniformly across the participating node set on every iteration.
  Design decision (documented, not "real" PageRank): nodes with total
  degree 0 (no incoming *and* no outgoing edges -- i.e. fully isolated
  files) are defined to have pagerank exactly 0.0 and are excluded from
  the power iteration entirely; the remaining "active" nodes' ranks are
  computed as a closed sub-system and sum to 1 among themselves, so the
  values over the *whole* file set still sum to 1. If the graph has edges
  nowhere at all (every node isolated), pagerank falls back to a uniform
  1/n over all nodes (there is no structure to concentrate rank into,
  and defining every node's rank as 0 would break the sum-to-1 contract).
* **betweenness** -- Brandes' algorithm run on the *directed* graph
  (shortest-path betweenness, unweighted), normalized by
  ``(n-1)(n-2)``. Guarded to all 0.0 when n < 3 (normalization undefined
  and no node can lie strictly between two others).
* **closeness** -- harmonic closeness computed over *incoming* directed
  paths: ``C(v) = (1/(n-1)) * sum_{u != v} 1/d(u, v)`` where ``d(u, v)``
  is the directed shortest-path distance from u to v (0 contribution if
  unreachable). Direction choice: this measures how quickly *other*
  files can reach ``v`` by following import chains (i.e. how central
  ``v`` is as an import *target* -- a widely-imported "core" utility
  scores high), as opposed to outgoing closeness (how quickly ``v``'s
  own imports fan out). Guarded to 0.0 when n < 2.
* **degree_centrality** -- ``(in_degree + out_degree) / (n - 1)`` over
  the deduplicated, self-loop-free graph. Guarded to 0.0 when n < 2.

``network_summary`` (graph-level):

* ``nodes`` -- total internal file count (n).
* ``edges`` -- deduplicated, self-loop-free internal import edge count (m).
* ``density`` -- ``m / (n * (n - 1))`` (directed graph density), 6dp;
  0.0 when n < 2.
* ``avg_degree`` -- ``m / n``, 6dp; 0.0 when n == 0.
* ``weakly_connected_components`` -- number of connected components when
  edges are treated as undirected; every node (including isolated ones)
  belongs to exactly one component.
* ``largest_component_size`` -- size (node count) of the largest such
  component; 0 when n == 0.
"""
from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeanalyzer.core.model import AnalysisResult

_DAMPING = 0.85
_MAX_ITER = 200
_TOL = 1e-10


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def _build_graph(result: "AnalysisResult") -> tuple[list[str], dict[str, set[str]], dict[str, set[str]], int]:
    nodes = sorted(f.id for f in result.files)
    node_set = set(nodes)
    adj: dict[str, set[str]] = {v: set() for v in nodes}
    radj: dict[str, set[str]] = {v: set() for v in nodes}
    edge_pairs: set[tuple[str, str]] = set()
    for e in result.edges:
        if e.kind != "import":
            continue
        if e.src not in node_set or e.dst not in node_set:
            continue
        if e.src == e.dst:
            continue
        edge_pairs.add((e.src, e.dst))
    for src, dst in edge_pairs:
        adj[src].add(dst)
        radj[dst].add(src)
    return nodes, adj, radj, len(edge_pairs)


# ---------------------------------------------------------------------------
# PageRank
# ---------------------------------------------------------------------------


def _pagerank_core(nodes: list[str], adj: dict[str, set[str]],
                    radj: dict[str, set[str]]) -> dict[str, float]:
    n = len(nodes)
    if n == 0:
        return {}
    rank = {v: 1.0 / n for v in nodes}
    for _ in range(_MAX_ITER):
        dangling_sum = sum(rank[v] for v in nodes if not adj.get(v))
        base = (1.0 - _DAMPING) / n
        new_rank: dict[str, float] = {}
        for v in nodes:
            incoming = sum(rank[u] / len(adj[u]) for u in sorted(radj.get(v, ())))
            new_rank[v] = base + _DAMPING * (incoming + dangling_sum / n)
        delta = sum(abs(new_rank[v] - rank[v]) for v in nodes)
        rank = new_rank
        if delta < _TOL:
            break
    return rank


def _pagerank(nodes: list[str], adj: dict[str, set[str]],
              radj: dict[str, set[str]], degree: dict[str, int]) -> dict[str, float]:
    n = len(nodes)
    if n == 0:
        return {}
    active = sorted(v for v in nodes if degree[v] > 0)
    if active:
        sub_adj = {v: adj[v] for v in active}
        sub_radj = {v: radj[v] for v in active}
        pr = _pagerank_core(active, sub_adj, sub_radj)
        return {v: pr.get(v, 0.0) for v in nodes}
    # No edges anywhere: fall back to a uniform distribution (no structure
    # to concentrate rank into; forcing everyone to 0.0 would break the
    # sum-to-1 contract).
    return {v: 1.0 / n for v in nodes}


# ---------------------------------------------------------------------------
# Betweenness (Brandes, directed)
# ---------------------------------------------------------------------------


def _betweenness(nodes: list[str], adj: dict[str, set[str]]) -> dict[str, float]:
    n = len(nodes)
    betweenness = {v: 0.0 for v in nodes}
    if n < 3:
        return betweenness

    for s in nodes:
        stack: list[str] = []
        preds: dict[str, list[str]] = {v: [] for v in nodes}
        sigma = {v: 0 for v in nodes}
        sigma[s] = 1
        dist = {v: -1 for v in nodes}
        dist[s] = 0
        q: deque[str] = deque([s])
        while q:
            v = q.popleft()
            stack.append(v)
            for w in sorted(adj.get(v, ())):
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    q.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    preds[w].append(v)

        delta = {v: 0.0 for v in nodes}
        while stack:
            w = stack.pop()
            for v in preds[w]:
                delta[v] += (sigma[v] / sigma[w]) * (1.0 + delta[w])
            if w != s:
                betweenness[w] += delta[w]

    scale = 1.0 / ((n - 1) * (n - 2))
    return {v: betweenness[v] * scale for v in nodes}


# ---------------------------------------------------------------------------
# Harmonic closeness over incoming directed paths
# ---------------------------------------------------------------------------


def _closeness(nodes: list[str], adj: dict[str, set[str]],
               radj: dict[str, set[str]]) -> dict[str, float]:
    n = len(nodes)
    closeness = {v: 0.0 for v in nodes}
    if n < 2:
        return closeness

    for v in nodes:
        dist = {v: 0}
        q: deque[str] = deque([v])
        while q:
            x = q.popleft()
            for u in sorted(radj.get(x, ())):
                if u not in dist:
                    dist[u] = dist[x] + 1
                    q.append(u)
        total = sum(1.0 / dist[u] for u in dist if u != v)
        closeness[v] = total / (n - 1)
    return closeness


# ---------------------------------------------------------------------------
# Degree centrality
# ---------------------------------------------------------------------------


def _degree_centrality(nodes: list[str], in_deg: dict[str, int],
                        out_deg: dict[str, int]) -> dict[str, float]:
    n = len(nodes)
    if n < 2:
        return {v: 0.0 for v in nodes}
    return {v: (in_deg[v] + out_deg[v]) / (n - 1) for v in nodes}


# ---------------------------------------------------------------------------
# Weakly connected components
# ---------------------------------------------------------------------------


def _weakly_connected_components(nodes: list[str], adj: dict[str, set[str]],
                                  radj: dict[str, set[str]]) -> tuple[int, int]:
    parent: dict[str, str] = {v: v for v in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for v in nodes:
        for w in adj.get(v, ()):
            union(v, w)
        for w in radj.get(v, ()):
            union(v, w)

    sizes: dict[str, int] = {}
    for v in nodes:
        root = find(v)
        sizes[root] = sizes.get(root, 0) + 1

    if not sizes:
        return 0, 0
    return len(sizes), max(sizes.values())


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_network_metrics(result: "AnalysisResult") -> tuple[dict[str, dict], dict]:
    """Return ``(per_file, network_summary)`` for the internal import graph.

    Pure function: does not mutate ``result``. See the module docstring for
    the exact metric definitions and design decisions.
    """
    nodes, adj, radj, m = _build_graph(result)
    n = len(nodes)

    in_deg = {v: len(radj[v]) for v in nodes}
    out_deg = {v: len(adj[v]) for v in nodes}
    degree = {v: in_deg[v] + out_deg[v] for v in nodes}

    pagerank = _pagerank(nodes, adj, radj, degree)
    betweenness = _betweenness(nodes, adj)
    closeness = _closeness(nodes, adj, radj)
    degree_centrality = _degree_centrality(nodes, in_deg, out_deg)

    per_file: dict[str, dict] = {}
    for v in nodes:
        per_file[v] = {
            "pagerank": round(pagerank.get(v, 0.0), 6),
            "betweenness": round(betweenness.get(v, 0.0), 6),
            "closeness": round(closeness.get(v, 0.0), 6),
            "degree_centrality": round(degree_centrality.get(v, 0.0), 6),
        }

    wcc, largest = _weakly_connected_components(nodes, adj, radj)

    network_summary = {
        "nodes": n,
        "edges": m,
        "density": round(m / (n * (n - 1)), 6) if n > 1 else 0.0,
        "avg_degree": round(m / n, 6) if n > 0 else 0.0,
        "weakly_connected_components": wcc,
        "largest_component_size": largest,
    }

    return per_file, network_summary
