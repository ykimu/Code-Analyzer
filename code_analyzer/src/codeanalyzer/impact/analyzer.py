"""Change-impact analysis (SPEC F6).

Given a file/line (or line range) inside an analyzed project, compute:

* the **origin** symbol (smallest enclosing definition, else the module),
* **backward impact** -- who transitively depends on the origin through
  reverse CALL / INHERIT / TYPE_REF edges, plus files that IMPORT the origin
  file (propagated at the module level),
* a **forward data-flow slice** -- variables defined in the queried range and
  where they are used, approximated one level across function boundaries in
  both directions (return -> callers, argument -> callee parameters),
* **unresolved boundaries** where traversal was cut short by dynamic dispatch,
  ambiguous name resolution, or unresolved callees.

The result is the ``ImpactResult`` dict documented at the bottom of
``core/model.py``.  Output is fully deterministic (stable sorting throughout).

Pure stdlib; reuses :mod:`codeanalyzer.core.graph` helpers where convenient but
keeps its own edge-aware traversal so it can record the reference lines and
per-path confidence that the plain hop-distance BFS does not carry.
"""
from __future__ import annotations

import os
from collections import defaultdict, deque
from pathlib import PurePosixPath
from typing import Any, Optional

from codeanalyzer.core.model import AnalysisResult, EdgeKind, SymbolKind

MODULE_SUFFIX = "::<module>"

_SYMBOL_EDGE_KINDS = {
    EdgeKind.CALL.value,
    EdgeKind.INHERIT.value,
    EdgeKind.TYPE_REF.value,
}

# via preference for a deterministic, semantically-ordered merge.  We keep the
# via of the FIRST cause (per SPEC) by processing sources in this fixed order:
# backward-call, import, then data-flow, and never overwriting an existing via.


# ---------------------------------------------------------------------------
# Origin resolution helpers
# ---------------------------------------------------------------------------
def _normalize_file(result: AnalysisResult, file: str) -> Optional[str]:
    """Map an arbitrary path spelling to a project file id, or ``None``.

    File ids are relative posix paths from the project root.  We accept the id
    itself, a path relative to the analysis root, an absolute path, and a
    unique suffix match.
    """
    ids = {f.id for f in result.files}
    if file in ids:
        return file

    # normalize separators to posix
    posix = PurePosixPath(file.replace("\\", "/")).as_posix()
    if posix in ids:
        return posix

    # relative to the recorded analysis root
    root = result.meta.root
    if root:
        try:
            rel = os.path.relpath(os.path.abspath(file), root)
            rel = PurePosixPath(rel.replace("\\", "/")).as_posix()
            if rel in ids:
                return rel
        except (ValueError, OSError):
            pass

    # unique suffix match (…/mod_c.py matching mod_c.py or a/b/mod_c.py)
    cands = sorted(
        i for i in ids
        if i == posix or i.endswith("/" + posix) or posix.endswith("/" + i)
    )
    if len(cands) == 1:
        return cands[0]
    return None


def _enclosing_symbol(result: AnalysisResult, fid: str,
                      start: int, end: int):
    """Smallest non-module symbol containing the range, else ``None``."""
    cands = [
        s for s in result.symbols
        if s.file_id == fid and s.kind != SymbolKind.MODULE.value
        and s.span.start_line <= start and s.span.end_line >= end
    ]
    if cands:
        return min(cands, key=lambda s: (s.span.end_line - s.span.start_line,
                                         s.id))
    # fall back to the innermost symbol at the start line
    return result.symbol_at(fid, start)


def _module_symbol(result: AnalysisResult, fid: str):
    mid = fid + MODULE_SUFFIX
    return next((s for s in result.symbols if s.id == mid), None)


# ---------------------------------------------------------------------------
# Edge-aware reverse traversal
# ---------------------------------------------------------------------------
def _reverse_walk(radj: dict[str, list], seeds, max_depth: int):
    """Breadth-first walk over reverse adjacency, edge aware.

    ``radj`` maps a node id to the list of edges whose ``dst`` is that node
    (so ``edge.src`` is a predecessor / dependent).  Seeds sit at hop 0 and are
    *not* returned.  Returns ``(info, truncated)`` where ``info`` maps each
    reached predecessor to ``{"hops", "lines", "path_certain"}`` and
    ``truncated`` is True when a node at the depth limit still had unexplored
    predecessors (i.e. ``max_hops_reached``).
    """
    info: dict[str, dict[str, Any]] = {}
    dist: dict[str, int] = {}
    truncated = False
    q: deque[tuple[str, int, bool]] = deque()
    for s in seeds:
        if s not in dist:
            dist[s] = 0
            q.append((s, 0, True))

    while q:
        node, hops, pcert = q.popleft()
        preds = radj.get(node)
        if hops >= max_depth:
            # anything beyond this node would exceed the depth budget
            if preds:
                for e in preds:
                    if e.src not in dist or dist[e.src] > max_depth:
                        truncated = True
                        break
            continue
        if not preds:
            continue
        for e in preds:
            pred = e.src
            edge_certain = (e.confidence == "certain") and (not e.ambiguous)
            npc = pcert and edge_certain
            nhops = hops + 1
            pi = info.get(pred)
            if pi is None:
                pi = {"hops": nhops, "lines": set(), "path_certain": npc}
                info[pred] = pi
            pi["lines"].add(e.line)
            if nhops < pi["hops"]:
                pi["hops"] = nhops
            pi["path_certain"] = pi["path_certain"] or npc
            if pred not in dist or dist[pred] > nhops:
                dist[pred] = nhops
                q.append((pred, nhops, npc))
    return info, truncated


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def analyze_impact(result: AnalysisResult, file: str, start_line: int,
                   end_line: int | None = None,
                   max_depth: int = 10) -> dict:
    """Compute the change-impact of a line range (see module docstring)."""
    start = int(start_line)
    end = int(end_line) if end_line is not None else start
    if end < start:
        start, end = end, start

    fid = _normalize_file(result, file)
    if fid is None:
        return {"error": f"unknown file: {file!r} is not part of the analyzed project"}

    query = {"file": fid, "start_line": start, "end_line": end,
             "depth": int(max_depth)}

    origin_sym = _enclosing_symbol(result, fid, start, end)
    origin_is_module = origin_sym is None
    if origin_is_module:
        origin_sym = _module_symbol(result, fid)
        if origin_sym is None:
            return {"error": f"file {fid!r} was not parsed; no symbols available",
                    "query": query}

    # indexes -------------------------------------------------------------
    sym_by_id = {s.id: s for s in result.symbols}
    defuse_of: dict[str, list] = defaultdict(list)
    for u in result.defuses:
        defuse_of[u.symbol_id].append(u)

    radj_sym: dict[str, list] = defaultdict(list)
    radj_file: dict[str, list] = defaultdict(list)
    fwd_calls_of: dict[str, list] = defaultdict(list)
    for e in result.edges:
        if e.kind in _SYMBOL_EDGE_KINDS:
            radj_sym[e.dst].append(e)
            if e.kind == EdgeKind.CALL.value:
                fwd_calls_of[e.src].append(e)
        elif e.kind == EdgeKind.IMPORT.value:
            radj_file[e.dst].append(e)
    for lst in radj_sym.values():
        lst.sort(key=lambda e: (e.src, e.line))
    for lst in radj_file.values():
        lst.sort(key=lambda e: (e.src, e.line))

    origin_defuses = defuse_of.get(origin_sym.id, [])

    # -- error: module-level line with no activity (blank/comment) --------
    if origin_is_module:
        has_activity = _module_activity(result, fid, start, end,
                                        origin_sym.id, origin_defuses)
        if not has_activity:
            return {"error": (f"no symbol or def-use activity at {fid}:"
                              f"{start}-{end} (blank or comment line?)"),
                    "query": query}

    # affected accumulator: symbol_id -> entry ----------------------------
    affected: dict[str, dict[str, Any]] = {}

    def _file_of(sid: str) -> str:
        s = sym_by_id.get(sid)
        if s is not None:
            return s.file_id
        if sid.endswith(MODULE_SUFFIX):
            return sid[: -len(MODULE_SUFFIX)]
        return sid.split("::", 1)[0]

    def _add(sid: str, lines, confidence: str, via: str, hops: int) -> None:
        entry = affected.get(sid)
        newlines = {ln for ln in lines if ln}
        if entry is None:
            affected[sid] = {
                "file_id": _file_of(sid),
                "lines": set(newlines),
                "confidence": confidence,
                "via": via,
                "hops": hops,
            }
            return
        entry["lines"].update(newlines)
        entry["hops"] = min(entry["hops"], hops)
        # keep strongest confidence (certain wins)
        if confidence == "certain":
            entry["confidence"] = "certain"
        # keep the via of the first cause (do not overwrite)

    # origin always present (hop 0, change site) --------------------------
    _add(origin_sym.id, range(start, end + 1), "certain", "dataflow", 0)

    # -- (2a) backward symbol traversal -----------------------------------
    seeds = [origin_sym.id]
    if origin_sym.kind == SymbolKind.CLASS.value:
        prefix = origin_sym.id + "::"
        seeds.extend(sorted(s.id for s in result.symbols
                            if s.id.startswith(prefix)))
    sym_info, sym_trunc = _reverse_walk(radj_sym, seeds, max_depth)
    for sid, pi in sym_info.items():
        conf = "certain" if pi["path_certain"] else "inferred"
        _add(sid, pi["lines"], conf, "call", pi["hops"])

    # -- (2b) import propagation (file level, module symbols) -------------
    file_info, file_trunc = _reverse_walk(radj_file, [fid], max_depth)
    for imp_fid, pi in file_info.items():
        conf = "certain" if pi["path_certain"] else "inferred"
        _add(imp_fid + MODULE_SUFFIX, pi["lines"], conf, "import", pi["hops"])

    # -- (3) forward data-flow slice --------------------------------------
    sliced_vars: list[str] = []
    origin_lines: set[int] = set()
    return_flow = False
    for u in origin_defuses:
        defs_in_range = [d for d in u.def_lines if start <= d <= end]
        if not defs_in_range:
            continue
        sliced_vars.append(u.var)
        # intra-procedural: uses of the variable within the same symbol
        origin_lines.update(defs_in_range)
        origin_lines.update(u.use_lines)
        # (3a) does the variable flow into a return at/after its def?
        if any(r >= min(defs_in_range) for r in u.return_lines):
            return_flow = True
    sliced_vars = sorted(set(sliced_vars))
    if origin_lines:
        _add(origin_sym.id, origin_lines, "certain", "dataflow", 0)

    # (3a) return -> callers: mark call sites in every caller of origin
    if return_flow:
        for e in radj_sym.get(origin_sym.id, []):
            if e.kind != EdgeKind.CALL.value:
                continue
            _add(e.src, [e.line], "inferred", "dataflow", 1)

    # (3b) argument -> callee parameters (one level)
    affected_line_set = set(origin_lines)
    for e in fwd_calls_of.get(origin_sym.id, []):
        if e.line not in affected_line_set:
            continue
        callee = e.dst
        if callee not in sym_by_id:
            continue  # external / unresolved callee (recorded as a boundary)
        param_lines: set[int] = set()
        for u in defuse_of.get(callee, []):
            if u.is_param:
                param_lines.update(u.def_lines)
                param_lines.update(u.use_lines)
        if param_lines:
            _add(callee, param_lines, "inferred", "dataflow", 1)

    # -- (4) unresolved boundaries ----------------------------------------
    visited = set(affected) | set(seeds) | {origin_sym.id}
    boundaries = _collect_boundaries(result.edges, visited)

    # -- assemble output --------------------------------------------------
    max_hops_reached = bool(sym_trunc or file_trunc)
    affected_files = _group_affected(affected)

    return {
        "query": query,
        "origin": {
            "symbol_id": origin_sym.id,
            "symbol_name": origin_sym.name,
            "kind": origin_sym.kind,
        },
        "affected_files": affected_files,
        "sliced_vars": sliced_vars,
        "unresolved_boundaries": boundaries,
        "stats": {
            "files": len(affected_files),
            "symbols": sum(len(af["symbols"]) for af in affected_files),
            "max_hops_reached": max_hops_reached,
        },
    }


def _module_activity(result: AnalysisResult, fid: str, start: int, end: int,
                     module_id: str, module_defuses) -> bool:
    """True if a module-level line range has any analyzable activity."""
    for e in result.edges:
        if e.line and start <= e.line <= end:
            if e.kind == EdgeKind.IMPORT.value and e.src == fid:
                return True
            if e.src == module_id:
                return True
    for u in module_defuses:
        for ln in (*u.def_lines, *u.use_lines, *u.return_lines):
            if start <= ln <= end:
                return True
    return False


def _collect_boundaries(edges, visited: set[str]) -> list[dict[str, Any]]:
    """Record unresolved / ambiguous / dynamic edges leaving visited symbols."""
    # count ambiguous candidates per (src, line)
    amb_groups: dict[tuple[str, int], set[str]] = defaultdict(set)
    for e in edges:
        if e.ambiguous:
            amb_groups[(e.src, e.line)].add(e.dst)

    seen: set[tuple[str, str, int]] = set()
    out: list[dict[str, Any]] = []
    for e in edges:
        if e.src not in visited:
            continue
        if e.kind == EdgeKind.IMPORT.value:
            continue
        reason: Optional[str] = None
        if e.confidence == "unresolved" or str(e.dst).startswith("unresolved:"):
            name = str(e.dst).split(":", 1)[1] if ":" in str(e.dst) else str(e.dst)
            reason = f"unresolved callee {name!r}"
        elif e.ambiguous:
            n = len(amb_groups[(e.src, e.line)])
            reason = f"ambiguous resolution ({n} candidates)"
        if reason is None:
            continue
        key = (e.src, reason, e.line)
        if key in seen:
            continue
        seen.add(key)
        out.append({"symbol_id": e.src, "reason": reason, "line": e.line})
    out.sort(key=lambda b: (b["symbol_id"], b["line"], b["reason"]))
    return out


def _group_affected(affected: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Group affected symbols by file with the required deterministic order."""
    by_file: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sid, entry in affected.items():
        by_file[entry["file_id"]].append({
            "symbol_id": sid,
            "lines": sorted(set(entry["lines"])),
            "confidence": entry["confidence"],
            "via": entry["via"],
            "hops": entry["hops"],
        })
    files_out: list[dict[str, Any]] = []
    for file_id, syms in by_file.items():
        syms.sort(key=lambda s: (s["hops"], s["symbol_id"]))
        min_hops = min(s["hops"] for s in syms)
        files_out.append({"file_id": file_id, "symbols": syms,
                          "_min_hops": min_hops})
    files_out.sort(key=lambda af: (af["_min_hops"], af["file_id"]))
    for af in files_out:
        del af["_min_hops"]
    return files_out


# ---------------------------------------------------------------------------
# Human-readable CLI rendering
# ---------------------------------------------------------------------------
def format_impact_text(impact: dict) -> str:
    """Render an ImpactResult (or error dict) as CLI text (Japanese labels)."""
    if "error" in impact:
        return f"エラー: {impact['error']}"

    lines: list[str] = []
    q = impact.get("query", {})
    o = impact.get("origin", {})
    rng = (f"{q.get('start_line')}"
           if q.get("start_line") == q.get("end_line")
           else f"{q.get('start_line')}-{q.get('end_line')}")
    lines.append(f"影響範囲解析: {q.get('file')}:{rng} (深度 {q.get('depth')})")
    lines.append(f"起点シンボル: {o.get('symbol_id')} "
                 f"[{o.get('kind')}] {o.get('symbol_name')}")

    sv = impact.get("sliced_vars", [])
    if sv:
        lines.append(f"スライス対象変数: {', '.join(sv)}")

    lines.append("")
    lines.append("影響を受けるファイル / シンボル:")
    via_label = {"call": "呼び出し", "import": "インポート",
                 "dataflow": "データフロー"}
    conf_label = {"certain": "確実", "inferred": "推定"}
    for af in impact.get("affected_files", []):
        lines.append(f"  {af['file_id']}")
        for s in af["symbols"]:
            lns = ",".join(str(x) for x in s["lines"]) or "-"
            lines.append(
                f"    - {s['symbol_id']}  "
                f"行:{lns}  "
                f"信頼度:{conf_label.get(s['confidence'], s['confidence'])}  "
                f"経路:{via_label.get(s['via'], s['via'])}  "
                f"hops:{s['hops']}"
            )

    boundaries = impact.get("unresolved_boundaries", [])
    if boundaries:
        lines.append("")
        lines.append("警告 (追跡打ち切り境界 / 偽陰性の可能性):")
        for b in boundaries:
            lines.append(f"  ! {b['symbol_id']} 行{b['line']}: {b['reason']}")

    st = impact.get("stats", {})
    lines.append("")
    footer = (f"統計: ファイル {st.get('files', 0)} / "
              f"シンボル {st.get('symbols', 0)}")
    if st.get("max_hops_reached"):
        footer += "  (深度上限に到達: 一部の依存は未探索)"
    lines.append(footer)
    return "\n".join(lines)
