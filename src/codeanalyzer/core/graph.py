"""Dependency graph utilities: adjacency, Tarjan SCC, reverse BFS.

Pure stdlib and fully iterative (no recursion depth limits).
"""
from __future__ import annotations

from collections import defaultdict, deque

from codeanalyzer.core.model import Edge, EdgeKind


class Graph:
    """Directed multigraph over string node ids with forward/reverse maps."""

    def __init__(self) -> None:
        self.forward: dict[str, set[str]] = defaultdict(set)
        self.reverse: dict[str, set[str]] = defaultdict(set)
        # (src, dst) -> set of edge kinds
        self.kinds: dict[tuple[str, str], set[str]] = defaultdict(set)
        self.nodes: set[str] = set()

    def add(self, src: str, dst: str, kind: str) -> None:
        self.forward[src].add(dst)
        self.reverse[dst].add(src)
        self.kinds[(src, dst)].add(kind)
        self.nodes.add(src)
        self.nodes.add(dst)

    def successors(self, node: str, kinds: set[str] | None = None):
        for dst in sorted(self.forward.get(node, ())):
            if kinds is None or self.kinds[(node, dst)] & kinds:
                yield dst

    def predecessors(self, node: str, kinds: set[str] | None = None):
        for src in sorted(self.reverse.get(node, ())):
            if kinds is None or self.kinds[(src, node)] & kinds:
                yield src


def build_graphs(edges: list[Edge]) -> tuple[Graph, Graph]:
    """Return (file_graph, symbol_graph).

    ``file_graph`` holds IMPORT edges (file->file/external/unresolved).
    ``symbol_graph`` holds CALL/INHERIT/TYPE_REF/DATAFLOW edges.
    """
    fg = Graph()
    sg = Graph()
    for e in edges:
        if e.kind == EdgeKind.IMPORT.value:
            fg.add(e.src, e.dst, e.kind)
        else:
            sg.add(e.src, e.dst, e.kind)
    return fg, sg


def tarjan_scc(graph: Graph, only_nodes: set[str] | None = None) -> list[list[str]]:
    """Iterative Tarjan SCC.  Returns cycles (components of size > 1),
    each sorted, list sorted deterministically.

    ``only_nodes`` restricts traversal to internal (project) nodes so that
    ``external:``/``unresolved:`` sinks do not create spurious structure.
    """
    index_counter = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    result: list[list[str]] = []

    def allowed(n: str) -> bool:
        return only_nodes is None or n in only_nodes

    nodes = sorted(n for n in graph.nodes if allowed(n))
    for start in nodes:
        if start in indices:
            continue
        # work stack of (node, iterator-position)
        work: list[tuple[str, list[str], int]] = []
        succ = [s for s in sorted(graph.forward.get(start, ())) if allowed(s)]
        indices[start] = lowlink[start] = index_counter
        index_counter += 1
        stack.append(start)
        on_stack[start] = True
        work.append((start, succ, 0))

        while work:
            node, successors, i = work[-1]
            if i < len(successors):
                work[-1] = (node, successors, i + 1)
                w = successors[i]
                if w not in indices:
                    indices[w] = lowlink[w] = index_counter
                    index_counter += 1
                    stack.append(w)
                    on_stack[w] = True
                    wsucc = [s for s in sorted(graph.forward.get(w, ()))
                             if allowed(s)]
                    work.append((w, wsucc, 0))
                elif on_stack.get(w):
                    lowlink[node] = min(lowlink[node], indices[w])
            else:
                work.pop()
                if lowlink[node] == indices[node]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        comp.append(w)
                        if w == node:
                            break
                    if len(comp) > 1:
                        result.append(sorted(comp))
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])

    result.sort()
    return result


def reverse_bfs(graph: Graph, start_ids, kinds=None,
                max_hops: int = 10) -> dict[str, int]:
    """Traverse the reverse graph from ``start_ids`` up to ``max_hops``.

    Returns {node_id: minimum hop distance}.  Start nodes are at hop 0.
    ``kinds`` optionally restricts which edge kinds may be traversed.
    """
    kset = set(kinds) if kinds else None
    dist: dict[str, int] = {}
    q: deque[tuple[str, int]] = deque()
    for s in start_ids:
        if s not in dist:
            dist[s] = 0
            q.append((s, 0))
    while q:
        node, hops = q.popleft()
        if hops >= max_hops:
            continue
        for pred in graph.predecessors(node, kset):
            if pred not in dist or dist[pred] > hops + 1:
                dist[pred] = hops + 1
                q.append((pred, hops + 1))
    return dist
