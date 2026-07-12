"""Tests for the CLI (SPEC F8/F9), invoked directly via ``main([...])``.

Uses the shared fixtures also exercised by the other workers' test suites:

* ``tests/fixtures/sample_project`` -- multi-language project with one
  intentionally broken .py file (the pipeline tolerates it, see
  ``test_pipeline.py``).
* ``tests/fixtures/impact_project`` -- small call-chain fixture purpose
  built for impact analysis (see ``test_impact.py``); ``mod_c.py::c`` is a
  real function starting at line 1, and line 4 is a blank module-level
  line with no analyzable activity (used by ``test_impact.py::
  test_blank_line_error``).
"""
from __future__ import annotations

import base64
import gzip
import json
import re
from pathlib import Path

import pytest

from codeanalyzer.cli import main
from codeanalyzer.core.model import TOOL_VERSION

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "sample_project"
IMPACT = FIXTURES / "impact_project"

#: report.html embeds the gzip+base64 analysis payload as a JS assignment;
#: this matches it so tests can decode and inspect the payload directly
#: rather than string-matching on the (compressed, unreadable) blob.
_B64_PAYLOAD_RE = re.compile(r'window\.__CA_B64__\s*=\s*"([^"]*)"')


def _decode_report_payload(html_text: str) -> dict:
    m = _B64_PAYLOAD_RE.search(html_text)
    assert m, "report.html is missing the window.__CA_B64__ payload assignment"
    raw = gzip.decompress(base64.b64decode(m.group(1)))
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------
def test_analyze_happy_path(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(["analyze", str(SAMPLE), "-o", str(out_dir), "-q"])
    assert rc == 0

    json_path = out_dir / "analysis.json"
    html_path = out_dir / "report.html"
    assert json_path.exists()
    assert html_path.exists()

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.1"
    assert len(data["metrics"]["files"]) > 0
    assert data["meta"]["generated_at"]  # ISO timestamp injected by the CLI

    html = html_path.read_text(encoding="utf-8")
    assert "__CA_DATA_B64_GZIP__" not in html


def test_analyze_json_only_skips_html(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(["analyze", str(SAMPLE), "-o", str(out_dir), "--json-only", "-q"])
    assert rc == 0
    assert (out_dir / "analysis.json").exists()
    assert not (out_dir / "report.html").exists()


def test_analyze_bad_path_exit_2(tmp_path, capsys):
    rc = main(["analyze", str(tmp_path / "does-not-exist"), "-q"])
    assert rc == 2
    assert capsys.readouterr().err


def test_analyze_prints_summary(tmp_path, capsys):
    out_dir = tmp_path / "out"
    rc = main(["analyze", str(SAMPLE), "-o", str(out_dir), "-q"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ファイル数" in out
    assert "パース失敗" in out
    assert "未解決インポート" in out
    assert str(out_dir / "analysis.json") in out


def test_analyze_default_out_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["analyze", str(SAMPLE), "--json-only", "-q"])
    assert rc == 0
    assert (tmp_path / "code-analyzer-out" / "analysis.json").exists()


def test_analyze_with_defuse_flag(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(["analyze", str(SAMPLE), "-o", str(out_dir), "--with-defuse",
               "--json-only", "-q"])
    assert rc == 0
    data = json.loads((out_dir / "analysis.json").read_text(encoding="utf-8"))
    assert "defuses" in data


def test_analyze_without_defuse_flag_omits_key(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(["analyze", str(SAMPLE), "-o", str(out_dir), "--json-only", "-q"])
    assert rc == 0
    data = json.loads((out_dir / "analysis.json").read_text(encoding="utf-8"))
    assert "defuses" not in data


def test_analyze_max_nodes_recorded(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(["analyze", str(SAMPLE), "-o", str(out_dir), "--max-nodes", "5",
               "--json-only", "-q"])
    assert rc == 0
    data = json.loads((out_dir / "analysis.json").read_text(encoding="utf-8"))
    assert data["metrics"]["ui"]["max_nodes"] == 5


def test_analyze_quiet_suppresses_progress(tmp_path, capsys):
    out_dir = tmp_path / "out"
    main(["analyze", str(SAMPLE), "-o", str(out_dir), "-q"])
    err = capsys.readouterr().err
    assert "走査" not in err
    assert "メトリクス計算" not in err


def test_analyze_verbose_shows_progress(tmp_path, capsys):
    out_dir = tmp_path / "out"
    main(["analyze", str(SAMPLE), "-o", str(out_dir)])
    err = capsys.readouterr().err
    assert "走査" in err


def test_analyze_embeds_sources_by_default(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(["analyze", str(SAMPLE), "-o", str(out_dir), "-q"])
    assert rc == 0
    html = (out_dir / "report.html").read_text(encoding="utf-8")
    data = _decode_report_payload(html)
    assert "sources" in data
    assert len(data["sources"]) > 0


def test_analyze_no_embed_sources_flag_omits_payload_sources(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(["analyze", str(SAMPLE), "-o", str(out_dir), "--no-embed-sources", "-q"])
    assert rc == 0
    html = (out_dir / "report.html").read_text(encoding="utf-8")
    data = _decode_report_payload(html)
    assert "sources" not in data


def test_analyze_no_embed_sources_produces_smaller_report(tmp_path):
    with_dir = tmp_path / "with_sources"
    without_dir = tmp_path / "without_sources"
    rc_with = main(["analyze", str(SAMPLE), "-o", str(with_dir), "-q"])
    rc_without = main(["analyze", str(SAMPLE), "-o", str(without_dir),
                        "--no-embed-sources", "-q"])
    assert rc_with == 0
    assert rc_without == 0
    size_with = (with_dir / "report.html").stat().st_size
    size_without = (without_dir / "report.html").stat().st_size
    assert size_with > size_without


# ---------------------------------------------------------------------------
# impact
# ---------------------------------------------------------------------------
def test_impact_happy_path(tmp_path, capsys):
    out_dir = tmp_path / "out"
    rc = main(["impact", str(IMPACT), "mod_c.py:2", "-o", str(out_dir), "-q"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "影響" in out

    data = json.loads((out_dir / "analysis.json").read_text(encoding="utf-8"))
    assert data["impact"] is not None
    assert data["impact"]["origin"]["symbol_id"] == "mod_c.py::c"
    assert (out_dir / "report.html").exists()


def test_impact_json_only(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(["impact", str(IMPACT), "mod_c.py:2", "-o", str(out_dir),
               "--json-only", "-q"])
    assert rc == 0
    assert (out_dir / "analysis.json").exists()
    assert not (out_dir / "report.html").exists()


def test_impact_line_range_target(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(["impact", str(IMPACT), "mod_a.py:4-6", "-o", str(out_dir),
               "--json-only", "-q"])
    assert rc == 0
    data = json.loads((out_dir / "analysis.json").read_text(encoding="utf-8"))
    assert data["impact"]["query"]["start_line"] == 4
    assert data["impact"]["query"]["end_line"] == 6


def test_impact_line_range_end_to_end(tmp_path, capsys):
    # v1.1: FILE:START-END range parsing exercised through the full pipeline
    # (JSON + HTML report), not just --json-only. mod_b.py:4-5 is the whole
    # body of `def b(n): return c(n)`.
    out_dir = tmp_path / "out"
    rc = main(["impact", str(IMPACT), "mod_b.py:4-5", "-o", str(out_dir), "-q"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "影響" in out

    json_path = out_dir / "analysis.json"
    html_path = out_dir / "report.html"
    assert json_path.exists()
    assert html_path.exists()

    data = json.loads(json_path.read_text(encoding="utf-8"))
    impact = data["impact"]
    assert impact is not None
    assert impact["query"]["file"] == "mod_b.py"
    assert impact["query"]["start_line"] == 4
    assert impact["query"]["end_line"] == 5
    assert impact["origin"]["symbol_id"] == "mod_b.py::b"


def test_impact_bad_target_format_exit_2(capsys):
    rc = main(["impact", str(IMPACT), "mod_c.py-no-colon", "-q"])
    assert rc == 2
    assert capsys.readouterr().err


def test_impact_blank_line_origin_exit_2(capsys):
    # mod_c.py line 4 is a blank module-level line (see test_impact.py).
    rc = main(["impact", str(IMPACT), "mod_c.py:4", "-q"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "エラー" in err


def test_impact_bad_path_exit_2(tmp_path):
    rc = main(["impact", str(tmp_path / "nope"), "mod_c.py:2", "-q"])
    assert rc == 2


def test_impact_unknown_file_exit_2(capsys):
    rc = main(["impact", str(IMPACT), "does_not_exist.py:1", "-q"])
    assert rc == 2
    assert capsys.readouterr().err


def test_impact_embeds_sources_by_default(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(["impact", str(IMPACT), "mod_c.py:2", "-o", str(out_dir), "-q"])
    assert rc == 0
    html = (out_dir / "report.html").read_text(encoding="utf-8")
    data = _decode_report_payload(html)
    assert "sources" in data
    assert data["impact"] is not None


def test_impact_no_embed_sources_flag_omits_payload_sources(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(["impact", str(IMPACT), "mod_c.py:2", "-o", str(out_dir),
               "--no-embed-sources", "-q"])
    assert rc == 0
    html = (out_dir / "report.html").read_text(encoding="utf-8")
    data = _decode_report_payload(html)
    assert "sources" not in data
    assert data["impact"] is not None


def test_impact_depth_flag_accepted(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(["impact", str(IMPACT), "mod_c.py:2", "--depth", "1",
               "-o", str(out_dir), "--json-only", "-q"])
    assert rc == 0
    data = json.loads((out_dir / "analysis.json").read_text(encoding="utf-8"))
    assert data["impact"]["query"]["depth"] == 1


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
def _parse_table_rows(out: str) -> list[dict[str, str]]:
    """Parse the aligned-text ``--format table`` output (columns are
    whitespace-padded and separated by 2+ spaces; a ``---`` separator line
    follows the header; the summary line + blank line trail the rows)."""
    lines = out.splitlines()
    header_idx = next(i for i, ln in enumerate(lines) if ln.startswith("path"))
    header = re.split(r"\s{2,}", lines[header_idx].strip())
    rows = []
    for ln in lines[header_idx + 2:]:
        if not ln.strip() or ln.strip().startswith("プロジェクト概要"):
            break
        cells = re.split(r"\s{2,}", ln.strip())
        rows.append(dict(zip(header, cells)))
    return rows


def test_metrics_table_format(capsys):
    rc = main(["metrics", str(SAMPLE)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "path" in out
    assert "loc_code" in out
    assert "プロジェクト概要" in out


def test_metrics_md_format(capsys):
    rc = main(["metrics", str(SAMPLE), "--format", "md"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("|")]
    assert len(lines) >= 2
    assert "cc_max" in lines[0]


def test_metrics_json_format(capsys):
    from codeanalyzer.metrics.export import FILE_COLUMNS

    rc = main(["metrics", str(SAMPLE), "--format", "json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert "files" in data
    assert "project" in data
    assert "network" in data
    assert "cycles" in data
    assert data["project"]["files"] == len(data["files"])
    first = data["files"][0]
    for col in FILE_COLUMNS:
        assert col in first
    assert "pagerank" in first


def test_metrics_csv_format_stdout(capsys):
    import csv
    import io

    rc = main(["metrics", str(SAMPLE), "--format", "csv"])
    assert rc == 0
    out = capsys.readouterr().out
    reader = csv.reader(io.StringIO(out))
    header = next(reader)
    assert "pagerank" in header
    assert "path" in header


def test_metrics_csv_format_output_file(tmp_path):
    out_file = tmp_path / "metrics.csv"
    rc = main(["metrics", str(SAMPLE), "--format", "csv", "--output", str(out_file)])
    assert rc == 0
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "pagerank" in content
    assert "# project summary" in content


def test_metrics_output_flag_suppresses_stdout(tmp_path, capsys):
    out_file = tmp_path / "metrics.json"
    rc = main(["metrics", str(SAMPLE), "--format", "json", "--output", str(out_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert out == ""
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert "files" in data


def test_metrics_output_flag_confirms_on_stderr(tmp_path, capsys):
    out_file = tmp_path / "metrics.md"
    rc = main(["metrics", str(SAMPLE), "--format", "md", "--output", str(out_file)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "出力" in err
    assert str(out_file) in err


def test_metrics_symbols_csv_writes_file(tmp_path):
    import csv

    sym_file = tmp_path / "symbols.csv"
    rc = main(["metrics", str(SAMPLE), "--symbols-csv", str(sym_file)])
    assert rc == 0
    assert sym_file.exists()
    rows = list(csv.reader(sym_file.open(encoding="utf-8", newline="")))
    assert rows[0] == ["symbol_id", "file", "name", "kind", "start_line",
                        "end_line", "loc", "cc", "params"]
    assert len(rows) > 1


def test_metrics_table_sort_by_cc_max_descending(capsys):
    rc = main(["metrics", str(SAMPLE), "--sort-by", "cc_max"])
    assert rc == 0
    out = capsys.readouterr().out
    rows = _parse_table_rows(out)
    cc_vals = [int(r["cc_max"]) for r in rows]
    assert cc_vals == sorted(cc_vals, reverse=True)


def test_metrics_table_top_limits_rows(capsys):
    rc = main(["metrics", str(SAMPLE), "--sort-by", "cc_max", "--top", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    rows = _parse_table_rows(out)
    assert len(rows) == 3


def test_metrics_csv_json_md_ignore_sort_and_top(capsys):
    # v1.1: --sort-by/--top apply ONLY to --format table; csv/md/json export
    # the full, unsorted-by-flag dataset via export_metrics().
    rc = main(["metrics", str(SAMPLE), "--format", "json",
               "--sort-by", "cc_max", "--top", "1"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    all_files_rc = main(["metrics", str(SAMPLE), "--format", "json"])
    assert all_files_rc == 0
    all_data = json.loads(capsys.readouterr().out)
    assert len(data["files"]) == len(all_data["files"])


def test_metrics_sort_by_path_default_ascending(capsys):
    rc = main(["metrics", str(SAMPLE)])
    out = capsys.readouterr().out
    rows = _parse_table_rows(out)
    paths = [r["path"] for r in rows]
    assert paths == sorted(paths)


def test_metrics_bad_sort_by_exit_2(capsys):
    rc = main(["metrics", str(SAMPLE), "--sort-by", "not_a_column"])
    assert rc == 2
    assert capsys.readouterr().err


def test_metrics_bad_path_exit_2(tmp_path):
    rc = main(["metrics", str(tmp_path / "nope")])
    assert rc == 2


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------
def test_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert TOOL_VERSION in out
    assert TOOL_VERSION == "1.2.0"


def test_no_command_prints_help_exit_2(capsys):
    rc = main([])
    assert rc == 2
    out = capsys.readouterr().out
    assert "usage" in out.lower()
