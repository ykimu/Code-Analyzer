"""Flat-file exports of ``result.metrics`` (SPEC F5 / v1.1 network metrics).

``export_metrics(result, fmt)`` renders the per-file metrics table (core
LOC/CC/MI/fan metrics plus the v1.1 network metrics), the project summary
(``metrics["project"]`` flattened with ``metrics["network"]`` under a
``network_*`` prefix), and the import cycles, in one of three formats:

* ``"csv"``  -- RFC4180 (via the stdlib ``csv`` module: CRLF line endings,
  quoting only where needed). Three sections in one file: the per-file
  table, a blank line, then a ``# project summary`` section (key,value
  rows), then a ``# cycles`` section (index, files joined by ``" -> "``).
* ``"md"``   -- the same three sections as GitHub-flavored Markdown tables
  (cycles rendered as a bullet list, since each cycle is a single
  variable-length item rather than tabular data).
* ``"json"`` -- ``{"project", "network", "files", "cycles"}``, files sorted
  by path, ``ensure_ascii=False``, 2-space indent.

Design decision: a file/symbol metrics entry may be missing a key (e.g. a
caller ran an older ``compute_metrics`` without network metrics, or is
exporting a partially-populated result) -- every accessor here uses
``dict.get`` with a default rather than ``[]``, so a missing key renders as
``""`` (csv/md) or ``null`` (json) and never raises ``KeyError``.

``export_symbols_csv(result)`` is a separate, single-purpose CSV export of
FUNCTION/METHOD symbols (``metrics["symbols"]`` is keyed by symbol id and
only covers those two kinds -- see ``metrics/engine.py``).
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from codeanalyzer.core.model import AnalysisResult, SymbolKind

# ---------------------------------------------------------------------------
# Shared column/row helpers
# ---------------------------------------------------------------------------

#: metric columns pulled from metrics["files"][fid], in header order.
_METRIC_COLUMNS: list[str] = [
    "loc_total", "loc_code", "loc_comment", "loc_blank",
    "functions", "classes", "cc_total", "cc_max", "mi",
    "fan_in", "fan_out", "fan_out_external",
    "pagerank", "betweenness", "closeness", "degree_centrality",
]

#: full per-file export column order (path/language come from FileInfo).
FILE_COLUMNS: list[str] = ["path", "language", *_METRIC_COLUMNS]

_SUPPORTED_FORMATS = ("csv", "md", "json")


def _file_rows(result: AnalysisResult) -> list[dict[str, Any]]:
    """One dict per file, sorted by path, with every FILE_COLUMNS key
    present (``None`` where the metric is missing)."""
    metrics_files = result.metrics.get("files", {})
    rows: list[dict[str, Any]] = []
    for f in sorted(result.files, key=lambda x: x.path):
        m = metrics_files.get(f.id, {})
        row: dict[str, Any] = {"path": f.path, "language": f.language}
        for col in _METRIC_COLUMNS:
            row[col] = m.get(col)
        rows.append(row)
    return rows


def _summary_pairs(result: AnalysisResult) -> list[tuple[str, Any]]:
    """Flattened (key, value) pairs: ``metrics["project"]`` keys as-is
    (nested dict values JSON-encoded), plus ``metrics["network"]`` keys
    under a ``network_`` prefix. Sorted by key for determinism."""
    project = result.metrics.get("project", {}) or {}
    network = result.metrics.get("network", {}) or {}
    pairs: list[tuple[str, Any]] = []
    for k in sorted(project.keys()):
        v = project[k]
        if isinstance(v, dict):
            v = json.dumps(v, sort_keys=True)
        pairs.append((k, v))
    for k in sorted(network.keys()):
        pairs.append((f"network_{k}", network[k]))
    return pairs


def _cycles(result: AnalysisResult) -> list[list[str]]:
    return result.metrics.get("cycles", []) or []


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def _render_csv(result: AnalysisResult) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(FILE_COLUMNS)
    for row in _file_rows(result):
        writer.writerow(["" if row[c] is None else row[c] for c in FILE_COLUMNS])

    writer.writerow([])
    writer.writerow(["# project summary"])
    writer.writerow(["key", "value"])
    for k, v in _summary_pairs(result):
        writer.writerow([k, "" if v is None else v])

    writer.writerow([])
    writer.writerow(["# cycles"])
    writer.writerow(["index", "files"])
    for idx, cyc in enumerate(_cycles(result)):
        writer.writerow([idx, " -> ".join(cyc)])

    return buf.getvalue()


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _md_escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_md_escape(c) for c in row) + " |")
    return "\n".join(lines)


def _render_md(result: AnalysisResult) -> str:
    files_table = _md_table(
        FILE_COLUMNS,
        [[row[c] for c in FILE_COLUMNS] for row in _file_rows(result)],
    )

    summary_table = _md_table(["key", "value"], [[k, v] for k, v in _summary_pairs(result)])

    cycles = _cycles(result)
    if cycles:
        cycles_lines = "\n".join(
            f"- Cycle {idx}: {' -> '.join(cyc)}" for idx, cyc in enumerate(cycles)
        )
    else:
        cycles_lines = "_(no cycles)_"

    return (
        "## Files\n\n" + files_table + "\n\n"
        "## Project Summary\n\n" + summary_table + "\n\n"
        "## Cycles\n\n" + cycles_lines + "\n"
    )


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def _render_json(result: AnalysisResult) -> str:
    payload = {
        "project": result.metrics.get("project"),
        "network": result.metrics.get("network"),
        "files": _file_rows(result),
        "cycles": _cycles(result),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def export_metrics(result: AnalysisResult, fmt: str) -> str:
    """Render ``result.metrics`` as ``fmt`` in {"csv", "md", "json"}."""
    if fmt == "csv":
        return _render_csv(result)
    if fmt == "md":
        return _render_md(result)
    if fmt == "json":
        return _render_json(result)
    raise ValueError(f"Unsupported export format: {fmt!r} (expected csv, md, or json)")


def export_symbols_csv(result: AnalysisResult) -> str:
    """CSV export of FUNCTION/METHOD symbols, sorted by symbol id.

    Columns: symbol_id,file,name,kind,start_line,end_line,loc,cc,params.
    ``loc``/``cc``/``params`` come from ``metrics["symbols"]`` and render
    as empty strings if that metrics entry is missing.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["symbol_id", "file", "name", "kind", "start_line",
                      "end_line", "loc", "cc", "params"])

    symbols_metrics = result.metrics.get("symbols", {})
    func_syms = [s for s in result.symbols
                 if s.kind in (SymbolKind.FUNCTION.value, SymbolKind.METHOD.value)]
    for s in sorted(func_syms, key=lambda x: x.id):
        m = symbols_metrics.get(s.id, {})
        writer.writerow([
            s.id, s.file_id, s.name, s.kind,
            s.span.start_line, s.span.end_line,
            m.get("loc", ""), m.get("cc", ""), m.get("params", ""),
        ])
    return buf.getvalue()
