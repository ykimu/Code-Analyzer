"""Tests for metrics/export.py (csv/md/json exports) and metrics/network.py
integration (v1.1 network metrics merged into metrics["files"]).
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from codeanalyzer.core.model import AnalysisResult, Edge, FileInfo, Span, Symbol
from codeanalyzer.core.pipeline import analyze_project
from codeanalyzer.metrics.engine import compute_metrics
from codeanalyzer.metrics.export import (
    FILE_COLUMNS,
    export_metrics,
    export_symbols_csv,
)

METRICS_FIXTURE = Path(__file__).parent / "fixtures" / "metrics_sample"


@pytest.fixture(scope="module")
def metrics_result():
    r = analyze_project(METRICS_FIXTURE, keep_sources=True)
    compute_metrics(r, r._sources)
    return r


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def test_csv_header_and_row_count(metrics_result):
    text = export_metrics(metrics_result, "csv")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)

    assert rows[0] == FILE_COLUMNS
    file_row_count = 0
    i = 1
    while rows[i]:
        file_row_count += 1
        i += 1
    assert file_row_count == len(metrics_result.files)
    assert i < len(rows)  # blank separator line was found


def test_csv_file_rows_sorted_by_path(metrics_result):
    text = export_metrics(metrics_result, "csv")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    paths = []
    for row in rows[1:]:
        if not row:
            break
        paths.append(row[0])
    assert paths == sorted(paths)


def test_csv_contains_project_summary_and_cycles_sections(metrics_result):
    text = export_metrics(metrics_result, "csv")
    assert "# project summary" in text
    assert "# cycles" in text


def test_csv_project_summary_has_network_prefixed_keys(metrics_result):
    text = export_metrics(metrics_result, "csv")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    keys = set()
    in_summary = False
    for row in rows:
        if row == ["# project summary"]:
            in_summary = True
            continue
        if row == ["# cycles"]:
            break
        if in_summary and row and row[0] != "key":
            keys.add(row[0])
    assert "network_nodes" in keys
    assert "network_density" in keys
    assert "files" in keys  # from metrics["project"]


def test_csv_missing_metric_keys_render_empty_string():
    r = AnalysisResult()
    r.files = [FileInfo(id="a.py", path="a.py", language="python")]
    # metrics["files"] intentionally left without an entry for a.py.
    r.metrics["files"] = {}
    r.metrics["project"] = {}
    r.metrics["network"] = {}
    r.metrics["cycles"] = []
    text = export_metrics(r, "csv")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    assert rows[0] == FILE_COLUMNS
    data_row = rows[1]
    assert data_row[0] == "a.py"
    assert data_row[1] == "python"
    for cell in data_row[2:]:
        assert cell == ""


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def test_md_contains_table_pipes_and_sections(metrics_result):
    text = export_metrics(metrics_result, "md")
    assert "## Files" in text
    assert "## Project Summary" in text
    assert "## Cycles" in text
    assert "|" in text
    # header separator row for a GFM table
    assert "| --- |" in text or "|---|" in text


def test_md_cycles_list_when_present():
    r = AnalysisResult()
    r.files = [FileInfo(id="a.py", path="a.py", language="python"),
               FileInfo(id="b.py", path="b.py", language="python")]
    r.metrics["files"] = {}
    r.metrics["project"] = {}
    r.metrics["network"] = {}
    r.metrics["cycles"] = [["a.py", "b.py"]]
    text = export_metrics(r, "md")
    assert "a.py -> b.py" in text


def test_md_no_cycles_placeholder():
    r = AnalysisResult()
    r.files = []
    r.metrics["files"] = {}
    r.metrics["project"] = {}
    r.metrics["network"] = {}
    r.metrics["cycles"] = []
    text = export_metrics(r, "md")
    assert "no cycles" in text.lower()


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def test_json_round_trips(metrics_result):
    text = export_metrics(metrics_result, "json")
    data = json.loads(text)
    assert set(data.keys()) == {"project", "network", "resolution", "files", "cycles"}
    assert data["network"]["nodes"] == 5
    assert len(data["files"]) == len(metrics_result.files)
    paths = [f["path"] for f in data["files"]]
    assert paths == sorted(paths)
    for f in data["files"]:
        assert set(f.keys()) == set(FILE_COLUMNS)


# ---------------------------------------------------------------------------
# Resolution (v1.6): project-level only, present in json/md, absent from csv
# ---------------------------------------------------------------------------


def test_json_includes_resolution_section(metrics_result):
    text = export_metrics(metrics_result, "json")
    data = json.loads(text)
    assert data["resolution"] == metrics_result.metrics["resolution"]
    assert set(data["resolution"].keys()) == {"calls", "imports", "by_language", "note"}


def test_json_resolution_null_when_absent():
    r = AnalysisResult()
    r.files = [FileInfo(id="a.py", path="a.py", language="python")]
    r.metrics["files"] = {}
    r.metrics["project"] = {}
    r.metrics["network"] = {}
    r.metrics["cycles"] = []
    text = export_metrics(r, "json")
    data = json.loads(text)
    assert data["resolution"] is None


def test_md_includes_resolution_section(metrics_result):
    text = export_metrics(metrics_result, "md")
    assert "## Resolution" in text
    assert "calls" in text
    assert "imports" in text
    assert "by_language" in text


def test_md_omits_resolution_section_when_absent():
    r = AnalysisResult()
    r.files = []
    r.metrics["files"] = {}
    r.metrics["project"] = {}
    r.metrics["network"] = {}
    r.metrics["cycles"] = []
    text = export_metrics(r, "md")
    assert "## Resolution" not in text


def test_csv_has_no_resolution_section(metrics_result):
    # CSV stays file-level only (SPEC: unchanged by v1.6).
    text = export_metrics(metrics_result, "csv")
    assert "resolution" not in text.lower()


def test_json_missing_metric_keys_are_null():
    r = AnalysisResult()
    r.files = [FileInfo(id="a.py", path="a.py", language="python")]
    r.metrics["files"] = {}
    r.metrics["project"] = {}
    r.metrics["network"] = {}
    r.metrics["cycles"] = []
    text = export_metrics(r, "json")
    data = json.loads(text)
    row = data["files"][0]
    assert row["path"] == "a.py"
    assert row["language"] == "python"
    for col in FILE_COLUMNS[2:]:
        assert row[col] is None


# ---------------------------------------------------------------------------
# Unsupported format
# ---------------------------------------------------------------------------


def test_unsupported_format_raises(metrics_result):
    with pytest.raises(ValueError):
        export_metrics(metrics_result, "xml")


# ---------------------------------------------------------------------------
# export_symbols_csv
# ---------------------------------------------------------------------------


def test_export_symbols_csv_header_and_sorted(metrics_result):
    text = export_symbols_csv(metrics_result)
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    assert rows[0] == ["symbol_id", "file", "name", "kind", "start_line",
                        "end_line", "loc", "cc", "params"]
    ids = [row[0] for row in rows[1:]]
    assert ids == sorted(ids)
    # only function/method symbols, matches metrics["symbols"] population.
    func_sym_ids = set(metrics_result.metrics["symbols"].keys())
    assert set(ids) == func_sym_ids


def test_export_symbols_csv_values_match_metrics(metrics_result):
    text = export_symbols_csv(metrics_result)
    reader = csv.DictReader(io.StringIO(text))
    rows = {row["symbol_id"]: row for row in reader}
    calc_row = rows["pkg/calc.py::calc"]
    assert calc_row["cc"] == "4"
    assert calc_row["params"] == "2"
    assert calc_row["file"] == "pkg/calc.py"


def test_export_symbols_csv_missing_metrics_empty():
    r = AnalysisResult()
    r.files = [FileInfo(id="a.py", path="a.py", language="python")]
    r.symbols = [Symbol(id="a.py::f", name="f", kind="function", file_id="a.py",
                         span=Span(start_line=1, end_line=2))]
    r.metrics["symbols"] = {}
    text = export_symbols_csv(r)
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["loc"] == ""
    assert rows[0]["cc"] == ""
    assert rows[0]["params"] == ""


# ---------------------------------------------------------------------------
# Integration: analyze_project + compute_metrics on the shared fixture
# ---------------------------------------------------------------------------


def test_integration_every_file_has_network_keys(metrics_result):
    file_ids = {f.id for f in metrics_result.files}
    assert metrics_result.metrics["network"]["nodes"] == len(file_ids)
    for fid in file_ids:
        m = metrics_result.metrics["files"][fid]
        for key in ("pagerank", "betweenness", "closeness", "degree_centrality"):
            assert key in m, (fid, key)
