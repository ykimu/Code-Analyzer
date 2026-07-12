"""End-to-end analysis pipeline: scan -> parse -> resolve -> graph.

Fills an :class:`AnalysisResult` with files, symbols, edges, def-use records,
diagnostics and metadata.  Metrics and impact are populated by their own
engines (other components).  ``meta.generated_at`` / ``git_revision`` are left
empty for the CLI to inject.  Output ordering is fully deterministic.
"""
from __future__ import annotations

from pathlib import Path

from codeanalyzer.core.graph import build_graphs, tarjan_scc
from codeanalyzer.core.model import (
    AnalysisResult,
    Diagnostic,
    FileInfo,
    Meta,
)
from codeanalyzer.core.scanner import default_excludes, scan
from codeanalyzer.parsers import parse_file
from codeanalyzer.resolvers.modules import resolve_modules
from codeanalyzer.resolvers.symbols import resolve_symbols


def analyze_project(
    root,
    includes: list[str] | None = None,
    excludes: list[str] | None = None,
    respect_gitignore: bool = True,
    compile_commands: str | None = None,
    keep_sources: bool = False,
) -> AnalysisResult:
    """Run the scan -> parse -> resolve -> graph pipeline.

    ``keep_sources`` (default ``False``, backward compatible): when set,
    attaches ``result._sources`` -- a ``{file_id: source_text}`` dict for
    every successfully scanned file -- so that callers such as the metrics
    engine (``metrics/engine.compute_metrics``) can re-derive LOC/CC/MI
    without re-reading files from disk. This is a plain dynamic attribute
    (not part of the dataclass schema / not serialized by ``to_dict``); it
    is only set when explicitly requested so existing callers are
    unaffected.
    """
    root = Path(root).resolve()
    includes = list(includes or [])
    excludes = list(excludes or [])

    result = AnalysisResult()
    diagnostics: list[Diagnostic] = []

    # -- scan -----------------------------------------------------------
    files, scan_diags = scan(root, includes, excludes, respect_gitignore)
    diagnostics.extend(scan_diags)

    # -- parse ----------------------------------------------------------
    outputs = {}
    file_infos: list[FileInfo] = []
    for sf in files:
        out, pdiags = parse_file(sf)
        outputs[sf.id] = out
        diagnostics.extend(pdiags)
        file_infos.append(FileInfo(
            id=sf.id,
            path=sf.id,
            language=sf.language.value,
            size_bytes=sf.size,
            is_test=sf.is_test,
            parse_ok=out.parse_ok,
            parse_partial=out.parse_partial,
        ))
        result.symbols.extend(out.symbols)
        result.defuses.extend(out.defuses)

    result.files = file_infos

    if keep_sources:
        result._sources = {sf.id: sf.source for sf in files}

    # -- resolve --------------------------------------------------------
    mod_res = resolve_modules(files, outputs, root, compile_commands)
    diagnostics.extend(mod_res.diagnostics)

    sym_edges = resolve_symbols(result.symbols, outputs, mod_res.bindings)

    result.edges = list(mod_res.edges) + list(sym_edges)
    result.diagnostics = diagnostics

    # -- graph (SCC cycles over the internal file import graph) ---------
    file_graph, _symbol_graph = build_graphs(result.edges)
    internal = {f.id for f in file_infos}
    cycles = tarjan_scc(file_graph, only_nodes=internal)
    result.metrics.setdefault("cycles", cycles)

    # -- meta -----------------------------------------------------------
    languages: dict[str, int] = {}
    for f in file_infos:
        languages[f.language] = languages.get(f.language, 0) + 1
    parse_failures = sum(1 for f in file_infos if not f.parse_ok)
    unresolved = sum(1 for d in diagnostics if d.kind == "unresolved_import")

    result.meta = Meta(
        root=str(root),
        excludes=sorted(set(default_excludes()) | set(excludes)),
        includes=includes,
        gitignore_respected=respect_gitignore,
        file_count=len(file_infos),
        parse_failures=parse_failures,
        unresolved_imports=unresolved,
        languages=dict(sorted(languages.items())),
    )

    # deterministic ordering
    result.files.sort(key=lambda f: f.id)
    result.symbols.sort(key=lambda s: s.id)
    result.edges.sort(key=lambda e: (e.src, e.dst, e.kind, e.line))
    result.defuses.sort(key=lambda u: (u.symbol_id, u.var))
    result.diagnostics.sort(key=lambda d: (d.file, d.kind, d.line))
    return result
