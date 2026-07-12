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
import shutil
import subprocess
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
    assert data["schema_version"] == "1.2"
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
# v1.3: impact --diff, compare subcommand, churn flags
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True, text=True)


def _build_git_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A one-commit git repo seeded from ``tests/fixtures/impact_project``
    (reuses the repo-building pattern from ``tests/test_diff_impact.py``)."""
    repo = tmp_path / name
    repo.mkdir()
    for src in sorted(IMPACT.iterdir()):
        if src.is_file():
            shutil.copy(src, repo / src.name)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tester@example.com")
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def test_impact_diff_worktree_happy_path(tmp_path, capsys):
    repo = _build_git_repo(tmp_path)
    # uncommitted edit inside the body of c() -- an analyzable hunk.
    (repo / "mod_c.py").write_text("def c(val):\n    result = val + 100\n    return result\n\n\nX = 1\n")

    out_dir = tmp_path / "out"
    rc = main(["impact", str(repo), "--diff", "-o", str(out_dir), "-q"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ハンク" in out

    data = json.loads((out_dir / "analysis.json").read_text(encoding="utf-8"))
    assert data["impact"]["query"]["diff"] == "worktree"
    assert data["impact"]["hunks"]


def test_impact_neither_target_nor_diff_exit_2(tmp_path, capsys):
    repo = _build_git_repo(tmp_path)
    rc = main(["impact", str(repo), "-q"])
    assert rc == 2
    assert "エラー" in capsys.readouterr().err


def test_impact_both_target_and_diff_exit_2(tmp_path, capsys):
    repo = _build_git_repo(tmp_path)
    rc = main(["impact", str(repo), "mod_c.py:2", "--diff", "-q"])
    assert rc == 2
    assert "エラー" in capsys.readouterr().err


def test_compare_happy_path(tmp_path):
    proj_a = tmp_path / "proj_a"
    shutil.copytree(SAMPLE, proj_a)
    out_a = tmp_path / "out_a"
    rc = main(["analyze", str(proj_a), "-o", str(out_a), "--no-churn", "--json-only", "-q"])
    assert rc == 0

    proj_b = tmp_path / "proj_b"
    shutil.copytree(SAMPLE, proj_b)
    # modify one file so the two analyses actually differ.
    target = proj_b / "py" / "models.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n\ndef extra_fn():\n    return 42\n")
    out_b = tmp_path / "out_b"
    rc = main(["analyze", str(proj_b), "-o", str(out_b), "--no-churn", "--json-only", "-q"])
    assert rc == 0

    cmp_out = tmp_path / "cmp_out"
    rc = main(["compare", str(out_a / "analysis.json"), str(out_b / "analysis.json"),
               "-o", str(cmp_out)])
    assert rc == 0

    cmp_json_path = cmp_out / "compare.json"
    cmp_html_path = cmp_out / "compare.html"
    assert cmp_json_path.exists()
    assert cmp_html_path.exists()

    cmp_data = json.loads(cmp_json_path.read_text(encoding="utf-8"))
    assert cmp_data["files"]["changed"]

    html = cmp_html_path.read_text(encoding="utf-8")
    assert "__CA_CMP_JSON__" not in html
    assert "__CA_EMPTY_TEXT__" not in html


def test_compare_json_only_skips_html(tmp_path):
    out_dir = tmp_path / "out"
    rc = main(["analyze", str(SAMPLE), "-o", str(out_dir), "--no-churn", "--json-only", "-q"])
    assert rc == 0

    cmp_out = tmp_path / "cmp_out"
    rc = main(["compare", str(out_dir / "analysis.json"), str(out_dir / "analysis.json"),
               "-o", str(cmp_out), "--json-only"])
    assert rc == 0
    assert (cmp_out / "compare.json").exists()
    assert not (cmp_out / "compare.html").exists()


def test_compare_corrupt_json_exit_2(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text("{}", encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json !!", encoding="utf-8")

    rc = main(["compare", str(bad), str(good)])
    assert rc == 2
    assert "エラー" in capsys.readouterr().err


def test_compare_missing_required_keys_exit_2(tmp_path, capsys):
    a = tmp_path / "a.json"
    a.write_text("{}", encoding="utf-8")
    b = tmp_path / "b.json"
    b.write_text("{}", encoding="utf-8")

    rc = main(["compare", str(a), str(b)])
    assert rc == 2
    assert "エラー" in capsys.readouterr().err


def test_analyze_no_churn_flag_omits_churn_keys(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE, repo)
    out_dir = tmp_path / "out"
    rc = main(["analyze", str(repo), "-o", str(out_dir), "--no-churn", "--json-only", "-q"])
    assert rc == 0
    data = json.loads((out_dir / "analysis.json").read_text(encoding="utf-8"))
    for fm in data["metrics"]["files"].values():
        assert "churn_commits" not in fm
        assert "hotspot" not in fm
    assert "hotspots" not in data["metrics"]


def test_analyze_churn_window_zero_behaves_like_no_churn(tmp_path):
    # Contract (model.py "v1.3 additions"): --churn-window 0 = churn
    # DISABLED -- collect_churn is never called, so no churn keys and no
    # metrics["churn"] section appear (identical to --no-churn), even on a
    # real git repo where a "--since 0 days" window would otherwise produce
    # meaningless all-zero rows.
    repo = _build_git_repo(tmp_path)
    out_dir = tmp_path / "out"
    rc = main(["analyze", str(repo), "-o", str(out_dir), "--churn-window", "0",
               "--json-only", "-q"])
    assert rc == 0
    data = json.loads((out_dir / "analysis.json").read_text(encoding="utf-8"))
    for fm in data["metrics"]["files"].values():
        assert "churn_commits" not in fm
        assert "hotspot" not in fm
    assert "churn" not in data["metrics"]
    assert "hotspots" not in data["metrics"]


def test_analyze_churn_all_full_history(tmp_path):
    # --churn-all = full history: churn runs with window_days=None, which
    # serializes as null in analysis.json.
    repo = _build_git_repo(tmp_path)
    out_dir = tmp_path / "out"
    rc = main(["analyze", str(repo), "-o", str(out_dir), "--churn-all",
               "--json-only", "-q"])
    assert rc == 0
    data = json.loads((out_dir / "analysis.json").read_text(encoding="utf-8"))
    churn = data["metrics"]["churn"]
    assert churn["available"] is True
    assert churn["window_days"] is None
    assert churn["commits_scanned"] >= 1


def test_analyze_negative_churn_window_exit_2(tmp_path, capsys):
    repo = tmp_path / "proj"
    shutil.copytree(SAMPLE, repo)
    rc = main(["analyze", str(repo), "--churn-window", "-5", "--json-only", "-q"])
    assert rc == 2
    assert "churn-window" in capsys.readouterr().err


def test_analyze_non_git_dir_churn_unavailable(tmp_path, capsys):
    # SAMPLE itself is not a git repo (nor is any tmp copy of it).
    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE, repo)
    out_dir = tmp_path / "out"
    rc = main(["analyze", str(repo), "-o", str(out_dir), "--json-only", "-q"])
    assert rc == 0
    data = json.loads((out_dir / "analysis.json").read_text(encoding="utf-8"))
    assert data["metrics"]["churn"]["available"] is False
    # summary suppression: no hotspot data -> no ホットスポット上位 line.
    out = capsys.readouterr().out
    assert "ホットスポット上位" not in out


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------
def test_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert TOOL_VERSION in out
    assert TOOL_VERSION == "1.5.0"


def test_no_command_prints_help_exit_2(capsys):
    rc = main([])
    assert rc == 2
    out = capsys.readouterr().out
    assert "usage" in out.lower()
