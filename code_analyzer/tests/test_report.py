"""Tests for the report-generation layer (json_writer.py / html_builder.py).

Builds a small synthetic AnalysisResult in-code (no dependency on the
scanner/pipeline, which is being developed in parallel by another worker;
only core.model is used, per the contract) and verifies:

  * json_writer.write_json produces a JSON file that round-trips back to
    the same dict AnalysisResult.to_dict() would produce.
  * html_builder.build_html produces a single self-contained HTML file
    with both placeholder tokens substituted, the compressed payload
    embedded, no external script/link tags, and a reasonable size.

As a side effect (see test_build_html_preview_sample), the built sample
report is also written to /home/claude/code_analyzer/preview_sample.html
for manual human inspection in a browser. The same fixture-building logic
is reused by scripts/make_preview.py for standalone regeneration.
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

from codeanalyzer.core.model import (
    SCHEMA_VERSION,
    AnalysisResult,
    DefUse,
    Diagnostic,
    Edge,
    FileInfo,
    Meta,
    Span,
    Symbol,
)
from codeanalyzer.report.html_builder import _MAX_SOURCE_FILE_CHARS, build_html
from codeanalyzer.report.json_writer import write_json

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "src" / "codeanalyzer" / "report" / "template" / "report_template.html"


def build_sample_result() -> AnalysisResult:
    """Construct a small, deterministic AnalysisResult covering:

    - 6 files across 3 languages (python, javascript, cpp)
    - ~10 symbols (functions/methods/classes)
    - import edges incl. one external ("external:requests") and one
      unresolved ("unresolved:missing_header")
    - call/inherit edges incl. one ambiguous edge
    - a metrics dict matching the shape documented in model.py's docstring
    - one file-level import cycle (app.py <-> models.py)
    - diagnostics of a few different kinds
    - impact = None (exercises the report's interactive impact mode)
    """
    files = [
        FileInfo(id="src/app.py", path="src/app.py", language="python", size_bytes=512),
        FileInfo(id="src/utils.py", path="src/utils.py", language="python", size_bytes=340),
        FileInfo(id="src/models.py", path="src/models.py", language="python", size_bytes=410),
        FileInfo(id="web/main.js", path="web/main.js", language="javascript", size_bytes=220),
        FileInfo(id="web/helpers.js", path="web/helpers.js", language="javascript", size_bytes=180),
        FileInfo(id="native/core.cpp", path="native/core.cpp", language="cpp", size_bytes=600,
                  parse_partial=True),
    ]

    symbols = [
        Symbol(id="src/app.py::main", name="main", kind="function", file_id="src/app.py",
               span=Span(1, 20)),
        Symbol(id="src/app.py::App", name="App", kind="class", file_id="src/app.py",
               span=Span(22, 40)),
        Symbol(id="src/app.py::App::run", name="run", kind="method", file_id="src/app.py",
               parent_id="src/app.py::App", span=Span(25, 39)),
        Symbol(id="src/utils.py::helper_a", name="helper_a", kind="function", file_id="src/utils.py",
               span=Span(1, 10)),
        Symbol(id="src/utils.py::helper_b", name="helper_b", kind="function", file_id="src/utils.py",
               span=Span(12, 25)),
        Symbol(id="src/models.py::BaseModel", name="BaseModel", kind="class", file_id="src/models.py",
               span=Span(1, 8)),
        Symbol(id="src/models.py::Model", name="Model", kind="class", file_id="src/models.py",
               span=Span(10, 30)),
        Symbol(id="src/models.py::Model::save", name="save", kind="method", file_id="src/models.py",
               parent_id="src/models.py::Model", span=Span(15, 29)),
        Symbol(id="web/main.js::init", name="init", kind="function", file_id="web/main.js",
               span=Span(1, 15)),
        Symbol(id="web/helpers.js::formatDate", name="formatDate", kind="function",
               file_id="web/helpers.js", span=Span(1, 9)),
        Symbol(id="native/core.cpp::compute", name="compute", kind="function",
               file_id="native/core.cpp", span=Span(5, 30)),
    ]

    edges = [
        # ---- file-level IMPORT edges ----
        Edge(src="src/app.py", dst="src/utils.py", kind="import", confidence="certain", line=1),
        Edge(src="src/app.py", dst="src/models.py", kind="import", confidence="certain", line=2),
        # cycle: app.py -> models.py -> app.py
        Edge(src="src/models.py", dst="src/app.py", kind="import", confidence="certain", line=1),
        Edge(src="web/main.js", dst="web/helpers.js", kind="import", confidence="certain", line=1),
        Edge(src="src/utils.py", dst="external:requests", kind="import", confidence="certain", line=1),
        Edge(src="native/core.cpp", dst="unresolved:missing_header", kind="import",
             confidence="unresolved", line=3),

        # ---- symbol-level CALL / INHERIT edges ----
        # reverse-traversal chain for the impact tab: save <- helper_b <- helper_a <- main
        Edge(src="src/app.py::main", dst="src/utils.py::helper_a", kind="call",
             confidence="certain", line=5),
        Edge(src="src/utils.py::helper_a", dst="src/utils.py::helper_b", kind="call",
             confidence="certain", line=4),
        Edge(src="src/utils.py::helper_b", dst="src/models.py::Model::save", kind="call",
             confidence="certain", line=18),
        Edge(src="src/app.py::App::run", dst="src/utils.py::helper_b", kind="call",
             confidence="certain", line=30),
        Edge(src="src/models.py::Model", dst="src/models.py::BaseModel", kind="inherit",
             confidence="certain", line=10),
        # ambiguous, name-based call resolution
        Edge(src="web/main.js::init", dst="web/helpers.js::formatDate", kind="call",
             confidence="inferred", ambiguous=True, line=6),
    ]

    diagnostics = [
        Diagnostic(file="native/core.cpp", kind="parse_partial",
                   message="tree-sitter reported ERROR nodes", line=0),
        Diagnostic(file="legacy/old_dump.js", kind="skipped_size",
                   message="File exceeds 5MB limit", line=0),
        Diagnostic(file="native/core.cpp", kind="unresolved_import",
                   message='cannot resolve #include "missing_header.h"', line=3),
    ]

    metrics = {
        "project": {
            "files": 6, "loc_total": 210, "loc_code": 165, "loc_comment": 20,
            "functions": 7, "classes": 3, "cc_avg": 2.4, "cc_max": 6, "mi_avg": 78.3,
            "cycles_count": 1,
            "languages": {
                "python": {"files": 3, "loc_code": 95},
                "javascript": {"files": 2, "loc_code": 40},
                "cpp": {"files": 1, "loc_code": 30},
            },
        },
        "files": {
            # v1.3: churn_commits/hotspot present on most files (git repo
            # case) -- native/core.cpp intentionally omits them too, on top
            # of already omitting the v1.1 centrality keys, to exercise the
            # report's "—" fallback for BOTH generations of optional metrics.
            "src/app.py": {"loc_total": 40, "loc_code": 32, "loc_comment": 5, "loc_blank": 3,
                            "functions": 1, "classes": 1, "cc_total": 5, "cc_max": 3, "mi": 82.1,
                            "fan_in": 1, "fan_out": 2, "fan_out_external": 0,
                            "pagerank": 0.182453, "betweenness": 0.100000,
                            "closeness": 0.416667, "degree_centrality": 0.6,
                            "churn_commits": 9, "churn_added": 40, "churn_deleted": 12,
                            "last_commit": "2026-07-01", "hotspot": 0.8571},
            "src/utils.py": {"loc_total": 25, "loc_code": 20, "loc_comment": 3, "loc_blank": 2,
                              "functions": 2, "classes": 0, "cc_total": 4, "cc_max": 2, "mi": 88.4,
                              "fan_in": 2, "fan_out": 1, "fan_out_external": 1,
                              "pagerank": 0.204112, "betweenness": 0.200000,
                              "closeness": 0.500000, "degree_centrality": 0.6,
                              "churn_commits": 5, "churn_added": 18, "churn_deleted": 4,
                              "last_commit": "2026-06-20", "hotspot": 0.4286},
            "src/models.py": {"loc_total": 30, "loc_code": 24, "loc_comment": 2, "loc_blank": 4,
                               "functions": 0, "classes": 2, "cc_total": 3, "cc_max": 2, "mi": 90.0,
                               "fan_in": 1, "fan_out": 1, "fan_out_external": 0,
                               "pagerank": 0.176220, "betweenness": 0.000000,
                               "closeness": 0.416667, "degree_centrality": 0.4,
                               "churn_commits": 2, "churn_added": 6, "churn_deleted": 1,
                               "last_commit": "2026-05-11", "hotspot": 0.1071},
            "web/main.js": {"loc_total": 20, "loc_code": 16, "loc_comment": 1, "loc_blank": 3,
                             "functions": 1, "classes": 0, "cc_total": 2, "cc_max": 2, "mi": 91.2,
                             "fan_in": 0, "fan_out": 1, "fan_out_external": 0,
                             "pagerank": 0.150000, "betweenness": 0.000000,
                             "closeness": 0.000000, "degree_centrality": 0.2,
                             "churn_commits": 0, "churn_added": 0, "churn_deleted": 0,
                             "last_commit": "", "hotspot": 0.0},
            "web/helpers.js": {"loc_total": 15, "loc_code": 12, "loc_comment": 1, "loc_blank": 2,
                                "functions": 1, "classes": 0, "cc_total": 1, "cc_max": 1, "mi": 95.5,
                                "fan_in": 1, "fan_out": 0, "fan_out_external": 0,
                                "pagerank": 0.143889, "betweenness": 0.000000,
                                "closeness": 0.000000, "degree_centrality": 0.2,
                                "churn_commits": 1, "churn_added": 3, "churn_deleted": 0,
                                "last_commit": "2026-04-02", "hotspot": 0.0357},
            # native/core.cpp intentionally omits the v1.1 centrality keys to
            # exercise the report's "—" fallback for missing metrics.
            "native/core.cpp": {"loc_total": 80, "loc_code": 61, "loc_comment": 8, "loc_blank": 11,
                                 "functions": 2, "classes": 0, "cc_total": 6, "cc_max": 6, "mi": 40.7,
                                 "fan_in": 0, "fan_out": 0, "fan_out_external": 0},
        },
        "symbols": {
            "src/app.py::main": {"loc": 20, "cc": 3, "params": 0},
            "src/app.py::App::run": {"loc": 15, "cc": 2, "params": 1},
            "src/utils.py::helper_a": {"loc": 10, "cc": 2, "params": 1},
            "src/utils.py::helper_b": {"loc": 14, "cc": 2, "params": 2},
            "src/models.py::Model::save": {"loc": 15, "cc": 2, "params": 1},
            "web/main.js::init": {"loc": 15, "cc": 2, "params": 0},
            "web/helpers.js::formatDate": {"loc": 9, "cc": 1, "params": 1},
            "native/core.cpp::compute": {"loc": 26, "cc": 6, "params": 3},
        },
        "cycles": [["src/app.py", "src/models.py"]],
        "network": {
            "nodes": 6, "edges": 6, "density": 0.2, "avg_degree": 2.0,
            "weakly_connected_components": 2, "largest_component_size": 4,
        },
        # v1.3 churn/hotspot additions (see core/model.py's "v1.3 additions"
        # docstring block) -- top-20 list sorted by hotspot desc, file_id asc.
        "churn": {"available": True, "window_days": 365, "commits_scanned": 17, "reason": None},
        "hotspots": [
            {"file_id": "src/app.py", "hotspot": 0.8571, "churn_commits": 9,
             "cc_total": 5, "loc_code": 32},
            {"file_id": "src/utils.py", "hotspot": 0.4286, "churn_commits": 5,
             "cc_total": 4, "loc_code": 20},
            {"file_id": "src/models.py", "hotspot": 0.1071, "churn_commits": 2,
             "cc_total": 3, "loc_code": 24},
            {"file_id": "web/helpers.js", "hotspot": 0.0357, "churn_commits": 1,
             "cc_total": 1, "loc_code": 12},
            {"file_id": "web/main.js", "hotspot": 0.0, "churn_commits": 0,
             "cc_total": 2, "loc_code": 16},
        ],
    }

    # DefUse records (v1.1): only present when include_defuse=True is
    # requested (JSON writer flag / HTML source-embedding path). Ties a
    # variable's definitions/uses to helper_a and helper_b so the 影響範囲
    # tab's client-side slicing (対象変数 / dataflow highlight) has
    # something concrete to exercise against.
    defuses = [
        DefUse(symbol_id="src/utils.py::helper_a", var="total", def_lines=[2],
               use_lines=[4, 6], return_lines=[6], is_param=False),
        DefUse(symbol_id="src/utils.py::helper_a", var="items", def_lines=[1],
               use_lines=[2, 3], is_param=True, param_index=0),
        DefUse(symbol_id="src/utils.py::helper_b", var="acc", def_lines=[13, 18],
               use_lines=[20, 24], return_lines=[24], is_param=False),
    ]

    meta = Meta(
        root="/tmp/sample_project",
        generated_at="2026-07-11T00:00:00Z",
        git_revision="abc1234",
        excludes=["node_modules", ".git", "__pycache__"],
        includes=[],
        gitignore_respected=True,
        file_count=6,
        parse_failures=0,
        unresolved_imports=1,
        languages={"python": 3, "javascript": 2, "cpp": 1},
    )

    return AnalysisResult(
        meta=meta,
        files=files,
        symbols=symbols,
        edges=edges,
        defuses=defuses,
        diagnostics=diagnostics,
        metrics=metrics,
        impact=None,
    )


def build_sample_sources() -> dict:
    """Synthetic {file_id: source_text} matching build_sample_result()'s
    files/symbols line spans closely enough to be useful in the 影響範囲
    tab's source viewer (line counts cover every symbol's span).
    """

    def numbered(prefix: str, n: int) -> str:
        return "\n".join(f"{prefix} line {i}" for i in range(1, n + 1)) + "\n"

    return {
        "src/app.py": numbered("# app.py", 40),
        "src/utils.py": numbered("# utils.py", 25),
        "src/models.py": numbered("# models.py", 30),
        "web/main.js": numbered("// main.js", 20),
        "web/helpers.js": numbered("// helpers.js", 15),
        "native/core.cpp": numbered("// core.cpp", 80),
    }


@pytest.fixture
def sample_result() -> AnalysisResult:
    return build_sample_result()


@pytest.fixture
def sample_result_with_sources() -> AnalysisResult:
    result = build_sample_result()
    result._sources = build_sample_sources()
    return result


def test_json_writer_round_trips(tmp_path, sample_result):
    out_path = tmp_path / "analysis.json"
    write_json(sample_result, out_path)

    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    loaded = json.loads(text)
    assert loaded == sample_result.to_dict()

    # Deterministic: writing again produces byte-identical output.
    out_path2 = tmp_path / "analysis2.json"
    write_json(sample_result, out_path2)
    assert out_path.read_text(encoding="utf-8") == out_path2.read_text(encoding="utf-8")

    # Basic schema sanity.
    assert loaded["schema_version"] == SCHEMA_VERSION == "1.2"
    assert len(loaded["files"]) == 6
    assert loaded["impact"] is None
    # include_defuse=False by default -> no "defuses" key.
    assert "defuses" not in loaded


def test_json_writer_include_defuse_flag(tmp_path, sample_result):
    out_path = tmp_path / "analysis_defuse.json"
    write_json(sample_result, out_path, include_defuse=True)
    loaded = json.loads(out_path.read_text(encoding="utf-8"))
    assert "defuses" in loaded
    assert len(loaded["defuses"]) == 3  # sample_result now carries 3 DefUse records
    assert {d["var"] for d in loaded["defuses"]} == {"total", "items", "acc"}


def test_build_html_self_contained(tmp_path, sample_result):
    out_path = tmp_path / "report.html"
    build_html(sample_result, out_path)

    assert out_path.exists()
    html = out_path.read_text(encoding="utf-8")

    # Placeholders must be fully substituted.
    assert "/*__D3_JS__*/" not in html
    assert "__CA_DATA_B64_GZIP__" not in html

    # No CDN / external resource loading anywhere in the document.
    assert "<script src=" not in html
    assert "<link href=" not in html

    # d3 was actually inlined (its source starts with a recognizable banner).
    assert "d3js.org" in html

    # The base64 gzip payload is embedded and decodes back to the original data.
    m = re.search(r'window\.__CA_B64__\s*=\s*"([A-Za-z0-9+/=]+)"', html)
    assert m is not None, "embedded data assignment not found"
    b64_payload = m.group(1)
    raw = gzip.decompress(base64.b64decode(b64_payload))
    decoded = json.loads(raw.decode("utf-8"))
    assert decoded == sample_result.to_dict(include_defuse=False)

    # Size guardrails (SPEC section 3: ~50MB ceiling; this tiny sample must
    # be far below that, and comfortably under the 5MB the test enforces).
    size = out_path.stat().st_size
    assert size < 5 * 1024 * 1024, f"report.html unexpectedly large: {size} bytes"


def _decode_payload(html: str) -> dict:
    m = re.search(r'window\.__CA_B64__\s*=\s*"([A-Za-z0-9+/=]+)"', html)
    assert m is not None, "embedded data assignment not found"
    raw = gzip.decompress(base64.b64decode(m.group(1)))
    return json.loads(raw.decode("utf-8"))


def test_build_html_embeds_sources_when_present(tmp_path, sample_result_with_sources):
    out_path = tmp_path / "report.html"
    build_html(sample_result_with_sources, out_path)
    decoded = _decode_payload(out_path.read_text(encoding="utf-8"))

    sources = build_sample_sources()
    assert "sources" in decoded
    assert set(decoded["sources"].keys()) == set(sources.keys())
    assert "defuses" in decoded  # sources path always embeds defuses too
    assert "sources_omitted" not in decoded  # nothing exceeded the guardrails
    assert decoded == sample_result_with_sources.to_dict(include_defuse=True, sources=sources)


def test_build_html_embed_sources_false_omits_sources(tmp_path, sample_result_with_sources):
    out_path = tmp_path / "report.html"
    build_html(sample_result_with_sources, out_path, embed_sources=False)
    decoded = _decode_payload(out_path.read_text(encoding="utf-8"))

    assert "sources" not in decoded
    assert "defuses" not in decoded
    assert decoded == sample_result_with_sources.to_dict(include_defuse=False)


def test_build_html_skips_oversized_source_into_sources_omitted(tmp_path, sample_result_with_sources):
    sources = build_sample_sources()
    sources["src/app.py"] = "x" * (_MAX_SOURCE_FILE_CHARS + 1)
    sample_result_with_sources._sources = sources

    out_path = tmp_path / "report.html"
    build_html(sample_result_with_sources, out_path)
    decoded = _decode_payload(out_path.read_text(encoding="utf-8"))

    assert "src/app.py" not in decoded["sources"]
    assert decoded["sources_omitted"] == ["src/app.py"]
    assert set(decoded["sources"].keys()) == set(sources.keys()) - {"src/app.py"}


def test_filter_sources_per_file_cap():
    from codeanalyzer.report.html_builder import _filter_sources

    sources = {"big": "x" * (_MAX_SOURCE_FILE_CHARS + 1), "small": "ok"}
    filtered, omitted = _filter_sources(sources)
    assert filtered == {"small": "ok"}
    assert omitted == ["big"]


def test_filter_sources_cumulative_cap(monkeypatch):
    # Shrink the caps so the cumulative-stop behavior can be tested with
    # tiny strings instead of allocating tens of megabytes of fixture data.
    import codeanalyzer.report.html_builder as html_builder_mod

    monkeypatch.setattr(html_builder_mod, "_MAX_SOURCE_FILE_CHARS", 1_000)
    monkeypatch.setattr(html_builder_mod, "_MAX_TOTAL_SOURCE_CHARS", 25)

    sources = {
        "a": "a" * 10,
        "b": "b" * 15,  # running total after b == 25, i.e. exactly at the cap -> still included
        "c": "c",       # running total after c == 26 > cap -> omitted, and everything after stops
        "d": "d",
    }
    filtered, omitted = html_builder_mod._filter_sources(sources)
    assert set(filtered.keys()) == {"a", "b"}
    assert omitted == ["c", "d"]


def test_build_html_missing_template_tokens_raise(tmp_path, sample_result, monkeypatch):
    import codeanalyzer.report.html_builder as html_builder_mod

    monkeypatch.setattr(html_builder_mod, "_read_template", lambda: "<html>no tokens here</html>")
    with pytest.raises(ValueError):
        build_html(sample_result, tmp_path / "broken.html")


def test_build_html_preview_sample(sample_result_with_sources):
    """Also writes the sample report to a fixed path for manual/browser
    inspection, per task instructions. Equivalent to running
    scripts/make_preview.py. Uses the sources-attached fixture so the
    preview exercises the 影響範囲 tab's interactive source viewer.
    """
    out_path = REPO_ROOT / "preview_sample.html"
    build_html(sample_result_with_sources, out_path)
    assert out_path.exists()
    assert out_path.stat().st_size > 0


def test_template_has_export_buttons_and_source_viewer():
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "CSVをダウンロード" in text
    assert "JSONをダウンロード" in text
    assert 'id="impact-source-viewer"' in text
    assert 'id="export-csv-btn"' in text
    assert 'id="export-json-btn"' in text


# ---------------------------------------------------------------------------
# v1.3: hotspot section / churn columns / treemap hotspot radio
# ---------------------------------------------------------------------------
def test_template_has_hotspot_markup():
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    # hotspot panel (scatter + top-20 table), hidden by default until JS
    # decides whether metrics.hotspots is non-empty.
    assert 'id="hotspot-panel"' in text
    assert 'id="hotspot-scatter-svg"' in text
    assert 'id="hotspot-table"' in text
    # file-metrics table gains the two new sortable columns.
    assert "churn_commits" in text
    assert "'hotspot'" in text or '"hotspot"' in text
    assert "変更回数" in text
    assert "ホットスポット" in text
    # treemap gains a third, conditionally-shown color-by option.
    assert 'id="treemap-hotspot-option"' in text
    assert "ホットスポットで色分け" in text
    # CSV/JSON export columns extended.
    assert "'churn_commits', 'hotspot'" in text


def test_hotspot_section_present_when_hotspots_data(tmp_path, sample_result):
    out_path = tmp_path / "report.html"
    build_html(sample_result, out_path)
    decoded = _decode_payload(out_path.read_text(encoding="utf-8"))
    assert decoded["metrics"]["hotspots"]
    assert decoded["metrics"]["files"]["src/app.py"]["hotspot"] == 0.8571
    assert decoded["metrics"]["files"]["src/app.py"]["churn_commits"] == 9


def test_hotspot_section_gracefully_absent_without_hotspots_data(tmp_path, sample_result):
    # A project with no git churn data (metrics.hotspots == [] / absent)
    # must still render -- the hotspot panel/treemap option are hidden
    # client-side, not omitted from the template.
    sample_result.metrics.pop("hotspots", None)
    sample_result.metrics.pop("churn", None)
    for fm in sample_result.metrics["files"].values():
        fm.pop("churn_commits", None)
        fm.pop("hotspot", None)
    out_path = tmp_path / "report_no_hotspots.html"
    build_html(sample_result, out_path)
    decoded = _decode_payload(out_path.read_text(encoding="utf-8"))
    assert not decoded["metrics"].get("hotspots")
    # the HTML itself is still well-formed / self-contained (no exception,
    # no leftover placeholder tokens).
    html = out_path.read_text(encoding="utf-8")
    assert "__CA_DATA_B64_GZIP__" not in html
    assert 'id="hotspot-panel"' in html  # markup exists; hiding is client-side


def test_template_inline_script_is_valid_js(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available in this environment")
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert scripts, "expected at least one inline <script> block"
    main_script = max(scripts, key=len)  # the big app script (vs. the tiny D3/data assignment ones)
    script_path = tmp_path / "inline.js"
    script_path.write_text(main_script, encoding="utf-8")
    result = subprocess.run(
        [node, "--check", str(script_path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
