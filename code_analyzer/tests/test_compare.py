"""Tests for report/compare.py (compare_analyses / build_compare_html).

Builds "old" and "new" analysis dicts by taking the synthetic fixture from
tests/test_report.py (build_sample_result().to_dict()) and applying a small,
explicit set of mutations on a deep copy: +1 file, -1 file, a changed
cc/mi pair on one existing file, one added IMPORT edge, one removed IMPORT
edge, one removed cycle, and a hotspot top-20 change. This exercises every
branch of the compare.json contract documented in core/model.py's "v1.3
additions" docstring block without depending on the scanner/pipeline
(consistent with test_report.py's own approach).
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_report import build_sample_result

from codeanalyzer.report.compare import build_compare_html, compare_analyses


# ---------------------------------------------------------------------------
# Fixture construction
# ---------------------------------------------------------------------------

def _base_dict() -> dict:
    return build_sample_result().to_dict()


def _with_hotspots(d: dict, entries: list) -> dict:
    d = copy.deepcopy(d)
    d["metrics"]["hotspots"] = entries
    return d


def build_old_new_pair() -> tuple[dict, dict]:
    """Returns (old, new) analysis dicts differing in every dimension the
    compare contract cares about."""
    old = _base_dict()
    new = copy.deepcopy(old)

    # ---- +1 file (added) ----
    new["files"].append({
        "id": "src/added_file.py", "path": "src/added_file.py", "language": "python",
        "size_bytes": 90, "is_test": False, "parse_ok": True, "parse_partial": False,
    })
    new["metrics"]["files"]["src/added_file.py"] = {
        "loc_total": 10, "loc_code": 8, "loc_comment": 1, "loc_blank": 1,
        "functions": 1, "classes": 0, "cc_total": 1, "cc_max": 1, "mi": 95.0,
        "fan_in": 0, "fan_out": 1, "fan_out_external": 1,
        "pagerank": 0.05, "betweenness": 0.0, "closeness": 0.0, "degree_centrality": 0.1,
    }

    # ---- -1 file (removed): web/helpers.js drops out of `new` ----
    new["files"] = [f for f in new["files"] if f["id"] != "web/helpers.js"]
    del new["metrics"]["files"]["web/helpers.js"]

    # ---- changed cc/mi on src/app.py (existing on both sides) ----
    new["metrics"]["files"]["src/app.py"] = dict(new["metrics"]["files"]["src/app.py"])
    new["metrics"]["files"]["src/app.py"]["cc_total"] = 9
    new["metrics"]["files"]["src/app.py"]["mi"] = 70.25
    # fan_out_external changes too, but must NOT show up in deltas (not a
    # compared key: not in the known list and doesn't match "loc_*").
    new["metrics"]["files"]["src/app.py"]["fan_out_external"] = 3
    # degree_centrality changes by an amount below the 1e-9 tolerance ->
    # must NOT be treated as a delta.
    new["metrics"]["files"]["src/app.py"]["degree_centrality"] = (
        new["metrics"]["files"]["src/app.py"]["degree_centrality"] + 1e-10
    )

    # ---- one IMPORT edge added, one IMPORT edge removed ----
    new["edges"] = [
        e for e in new["edges"]
        if not (e["src"] == "web/main.js" and e["dst"] == "web/helpers.js" and e["kind"] == "import")
    ]
    new["edges"].append({
        "src": "src/added_file.py", "dst": "src/utils.py", "kind": "import",
        "confidence": "certain", "ambiguous": False, "line": 1,
    })

    # ---- one cycle removed ----
    new["metrics"]["cycles"] = []

    # ---- project-level delta (cc_avg changes; other metrics unchanged) ----
    new["metrics"]["project"] = dict(new["metrics"]["project"])
    new["metrics"]["project"]["cc_avg"] = 3.1
    # mi_avg changes by an amount below tolerance -> must not appear.
    new["metrics"]["project"]["mi_avg"] = new["metrics"]["project"]["mi_avg"] + 1e-10

    # ---- hotspots: app.py resolved, models.py new, utils.py unchanged (present both sides) ----
    old_hotspots = [
        {"file_id": "src/app.py", "hotspot": 0.80, "churn_commits": 10, "cc_total": 5, "loc_code": 32},
        {"file_id": "src/utils.py", "hotspot": 0.60, "churn_commits": 5, "cc_total": 4, "loc_code": 20},
    ]
    new_hotspots = [
        {"file_id": "src/models.py", "hotspot": 0.55, "churn_commits": 4, "cc_total": 3, "loc_code": 24},
        {"file_id": "src/utils.py", "hotspot": 0.65, "churn_commits": 6, "cc_total": 4, "loc_code": 20},
    ]
    old = _with_hotspots(old, old_hotspots)
    new = _with_hotspots(new, new_hotspots)

    return old, new


@pytest.fixture
def old_new():
    return build_old_new_pair()


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------

def test_contract_top_level_keys(old_new):
    old, new = old_new
    cmp = compare_analyses(old, new)
    assert set(cmp.keys()) == {
        "schema_version", "kind", "old_meta", "new_meta",
        "project_delta", "files", "edges", "cycles", "hotspots",
    }
    assert cmp["kind"] == "comparison"
    assert cmp["schema_version"] == new["schema_version"]
    assert cmp["old_meta"] == old["meta"]
    assert cmp["new_meta"] == new["meta"]


def test_contract_nested_keys(old_new):
    old, new = old_new
    cmp = compare_analyses(old, new)
    assert set(cmp["files"].keys()) == {"added", "removed", "changed"}
    assert set(cmp["edges"].keys()) == {"added", "removed"}
    assert set(cmp["cycles"].keys()) == {"added", "removed"}
    assert set(cmp["hotspots"].keys()) == {"new", "resolved"}


def _without_hotspots(d: dict) -> dict:
    # build_sample_result() (tests/test_report.py) carries hotspot data by
    # default (v1.3) -- strip it to exercise the "absent on either side" case.
    d = copy.deepcopy(d)
    d["metrics"].pop("hotspots", None)
    return d


def test_hotspots_omitted_when_absent_on_either_side():
    old = _without_hotspots(_base_dict())
    new = copy.deepcopy(old)
    new["metrics"]["project"]["cc_avg"] = 999.0  # force some other delta so cmp isn't trivially empty
    cmp = compare_analyses(old, new)
    assert "hotspots" not in cmp

    # Also omitted when only one side has hotspot data.
    old_with = _with_hotspots(old, [{"file_id": "src/app.py", "hotspot": 0.5,
                                       "churn_commits": 1, "cc_total": 5, "loc_code": 32}])
    cmp2 = compare_analyses(old_with, new)
    assert "hotspots" not in cmp2


# ---------------------------------------------------------------------------
# project_delta
# ---------------------------------------------------------------------------

def test_project_delta_only_changed_metrics(old_new):
    old, new = old_new
    cmp = compare_analyses(old, new)
    assert cmp["project_delta"] == {"cc_avg": [2.4, 3.1]}
    # mi_avg differs by 1e-10 (below tolerance) -> absent.
    assert "mi_avg" not in cmp["project_delta"]
    # "files" count unaffected by the +1/-1 file swap -> absent.
    assert "files" not in cmp["project_delta"]
    # languages dict is never compared.
    assert "languages" not in cmp["project_delta"]


def test_project_delta_numeric_tolerance():
    old = _base_dict()
    new = copy.deepcopy(old)
    new["metrics"]["project"]["cc_avg"] = old["metrics"]["project"]["cc_avg"] + 1e-10
    cmp = compare_analyses(old, new)
    assert cmp["project_delta"] == {}

    new["metrics"]["project"]["cc_avg"] = old["metrics"]["project"]["cc_avg"] + 1e-8
    cmp2 = compare_analyses(old, new)
    assert "cc_avg" in cmp2["project_delta"]


# ---------------------------------------------------------------------------
# files: added / removed / changed
# ---------------------------------------------------------------------------

def test_files_added_removed(old_new):
    old, new = old_new
    cmp = compare_analyses(old, new)
    assert cmp["files"]["added"] == ["src/added_file.py"]
    assert cmp["files"]["removed"] == ["web/helpers.js"]


def test_files_changed_only_lists_files_with_deltas(old_new):
    old, new = old_new
    cmp = compare_analyses(old, new)
    changed = {c["file_id"]: c["deltas"] for c in cmp["files"]["changed"]}
    assert "src/app.py" in changed
    deltas = changed["src/app.py"]
    assert deltas["cc_total"] == [5, 9]
    assert deltas["mi"] == [82.1, 70.25]
    # fan_out_external is not a compared metric -> never appears.
    assert "fan_out_external" not in deltas
    # degree_centrality changed by < 1e-9 -> not a delta.
    assert "degree_centrality" not in deltas

    # Files with zero deltas are not included in "changed" at all.
    unchanged_ids = {f["id"] for f in old["files"]} & {f["id"] for f in new["files"]}
    unchanged_ids -= {"src/app.py"}
    assert unchanged_ids.isdisjoint(changed.keys())


def test_files_changed_entry_shape(old_new):
    old, new = old_new
    cmp = compare_analyses(old, new)
    for entry in cmp["files"]["changed"]:
        assert set(entry.keys()) == {"file_id", "deltas"}
        assert len(entry["deltas"]) >= 1
        for metric, pair in entry["deltas"].items():
            assert len(pair) == 2


# ---------------------------------------------------------------------------
# edges (IMPORT only)
# ---------------------------------------------------------------------------

def test_edges_added_removed(old_new):
    old, new = old_new
    cmp = compare_analyses(old, new)
    assert cmp["edges"]["added"] == [
        {"src": "src/added_file.py", "dst": "src/utils.py", "kind": "import"},
    ]
    assert cmp["edges"]["removed"] == [
        {"src": "web/main.js", "dst": "web/helpers.js", "kind": "import"},
    ]


def test_edges_ignore_non_import_kinds_and_line_confidence():
    old = _base_dict()
    new = copy.deepcopy(old)
    # Change the line/confidence of an existing import edge -> should NOT
    # register as added/removed (dedup key is (src,dst,kind) only).
    for e in new["edges"]:
        if e["kind"] == "import" and e["src"] == "src/app.py" and e["dst"] == "src/utils.py":
            e["line"] = 999
            e["confidence"] = "inferred"
    cmp = compare_analyses(old, new)
    assert cmp["edges"]["added"] == []
    assert cmp["edges"]["removed"] == []

    # A changed CALL edge (non-import) must never surface in edges diff.
    new2 = copy.deepcopy(old)
    new2["edges"].append({
        "src": "src/app.py::main", "dst": "src/models.py::Model::save", "kind": "call",
        "confidence": "certain", "ambiguous": False, "line": 42,
    })
    cmp2 = compare_analyses(old, new2)
    assert cmp2["edges"]["added"] == []


# ---------------------------------------------------------------------------
# cycles (rotation invariance)
# ---------------------------------------------------------------------------

def test_cycles_removed(old_new):
    old, new = old_new
    cmp = compare_analyses(old, new)
    assert cmp["cycles"]["removed"] == [["src/app.py", "src/models.py"]]
    assert cmp["cycles"]["added"] == []


def test_cycles_rotation_invariance():
    old = _base_dict()
    old["metrics"]["cycles"] = [["a.py", "b.py", "c.py"]]
    new = copy.deepcopy(old)
    # Same cycle, rotated (and using a different member ordering entirely)
    # -> must NOT show up as added or removed.
    new["metrics"]["cycles"] = [["b.py", "c.py", "a.py"]]
    cmp = compare_analyses(old, new)
    assert cmp["cycles"]["added"] == []
    assert cmp["cycles"]["removed"] == []


def test_cycles_add_and_remove_distinct_cycles():
    old = _base_dict()
    old["metrics"]["cycles"] = [["a.py", "b.py"]]
    new = copy.deepcopy(old)
    new["metrics"]["cycles"] = [["c.py", "d.py"]]
    cmp = compare_analyses(old, new)
    assert cmp["cycles"]["added"] == [["c.py", "d.py"]]
    assert cmp["cycles"]["removed"] == [["a.py", "b.py"]]


# ---------------------------------------------------------------------------
# hotspots
# ---------------------------------------------------------------------------

def test_hotspots_new_and_resolved(old_new):
    old, new = old_new
    cmp = compare_analyses(old, new)
    new_ids = {e["file_id"] for e in cmp["hotspots"]["new"]}
    resolved_ids = {e["file_id"] for e in cmp["hotspots"]["resolved"]}
    assert new_ids == {"src/models.py"}
    assert resolved_ids == {"src/app.py"}
    # utils.py is present (unchanged membership) on both sides -> neither list.
    assert "src/utils.py" not in new_ids
    assert "src/utils.py" not in resolved_ids
    # entries carry the full hotspot object from their originating side.
    resolved_entry = next(e for e in cmp["hotspots"]["resolved"] if e["file_id"] == "src/app.py")
    assert resolved_entry == {"file_id": "src/app.py", "hotspot": 0.80, "churn_commits": 10,
                               "cc_total": 5, "loc_code": 32}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("missing_key", ["schema_version", "files", "metrics"])
def test_missing_required_key_raises(missing_key):
    good = _base_dict()

    bad_new = _base_dict()
    del bad_new[missing_key]
    with pytest.raises(ValueError):
        compare_analyses(good, bad_new)

    bad_old = _base_dict()
    del bad_old[missing_key]
    with pytest.raises(ValueError):
        compare_analyses(bad_old, good)


def test_non_dict_input_raises():
    old = _base_dict()
    with pytest.raises(ValueError):
        compare_analyses(old, ["not", "a", "dict"])
    with pytest.raises(ValueError):
        compare_analyses(None, old)


# ---------------------------------------------------------------------------
# Determinism / round-trip
# ---------------------------------------------------------------------------

def test_determinism(old_new):
    old, new = old_new
    cmp1 = compare_analyses(old, new)
    cmp2 = compare_analyses(copy.deepcopy(old), copy.deepcopy(new))
    assert cmp1 == cmp2
    assert json.dumps(cmp1, sort_keys=True) == json.dumps(cmp2, sort_keys=True)


def test_json_round_trip(old_new):
    old, new = old_new
    cmp = compare_analyses(old, new)
    text = json.dumps(cmp)
    reparsed = json.loads(text)
    assert reparsed == cmp


# ---------------------------------------------------------------------------
# build_compare_html
# ---------------------------------------------------------------------------

def test_build_compare_html_basic(tmp_path, old_new):
    old, new = old_new
    cmp = compare_analyses(old, new)
    out_path = tmp_path / "compare.html"
    build_compare_html(cmp, out_path)

    assert out_path.exists()
    html = out_path.read_text(encoding="utf-8")

    # No leftover placeholder tokens.
    assert "__CA_CMP_JSON__" not in html
    assert "__CA_EMPTY_TEXT__" not in html

    # No external resources (fully self-contained, offline-usable).
    assert "<script src=" not in html
    assert "<link href=" not in html
    assert "http://" not in html
    assert "https://" not in html

    # The embedded JSON payload round-trips back to the compare dict.
    m = re.search(r"window\.__CA_CMP__\s*=\s*(\{.*?\});\s*</script>", html, re.S)
    assert m is not None, "embedded compare payload not found"
    decoded = json.loads(m.group(1))
    assert decoded == cmp

    # This fixture pair has real differences -> no "no differences" headline.
    assert "差分はありません" not in html


def test_build_compare_html_identical_inputs_shows_no_diff_state(tmp_path):
    old = _base_dict()
    new = _base_dict()  # exact duplicate -> compare dict has no differences
    cmp = compare_analyses(old, new)

    # Sanity: an identical pair really does produce an "empty" comparison.
    assert cmp["project_delta"] == {}
    assert cmp["files"]["added"] == cmp["files"]["removed"] == cmp["files"]["changed"] == []
    assert cmp["edges"]["added"] == cmp["edges"]["removed"] == []
    assert cmp["cycles"]["added"] == cmp["cycles"]["removed"] == []

    out_path = tmp_path / "compare_identical.html"
    build_compare_html(cmp, out_path)
    html = out_path.read_text(encoding="utf-8")
    assert html.count("差分はありません") == 1
    assert 'id="empty-state"' in html


def test_build_compare_html_non_identical_does_not_show_no_diff_state(tmp_path, old_new):
    """Guard against a template regression where the "no differences" copy
    would render regardless of content: it must be server-side conditional
    on `cmp` actually being empty, not a static string always present in
    the output."""
    old, new = old_new
    cmp = compare_analyses(old, new)
    out_path = tmp_path / "compare_nonempty.html"
    build_compare_html(cmp, out_path)
    html = out_path.read_text(encoding="utf-8")
    assert cmp["project_delta"]  # sanity: this fixture pair truly differs
    assert "差分はありません" not in html


def test_build_compare_html_missing_token_raises(tmp_path, old_new, monkeypatch):
    import codeanalyzer.report.compare as compare_mod

    old, new = old_new
    cmp = compare_analyses(old, new)
    monkeypatch.setattr(compare_mod, "_read_compare_template", lambda: "<html>no token here</html>")
    with pytest.raises(ValueError):
        build_compare_html(cmp, tmp_path / "broken.html")


def test_build_compare_html_escapes_closing_script_sequences(tmp_path):
    old = _base_dict()
    new = copy.deepcopy(old)
    # Inject a file id containing "</script>" to make sure the JSON encoder
    # neutralizes it before embedding.
    tricky_id = "weird</script><script>alert(1)//.py"
    new["files"].append({
        "id": tricky_id, "path": tricky_id, "language": "python",
        "size_bytes": 1, "is_test": False, "parse_ok": True, "parse_partial": False,
    })
    cmp = compare_analyses(old, new)
    assert tricky_id in cmp["files"]["added"]

    out_path = tmp_path / "compare_tricky.html"
    build_compare_html(cmp, out_path)
    html = out_path.read_text(encoding="utf-8")
    assert "</script><script>alert" not in html
    # But the (escaped) id is still recoverable from the payload.
    m = re.search(r"window\.__CA_CMP__\s*=\s*(\{.*?\});\s*</script>", html, re.S)
    decoded = json.loads(m.group(1))
    assert tricky_id in decoded["files"]["added"]


def test_compare_template_inline_script_is_valid_js():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available in this environment")
    template_path = (
        Path(__file__).resolve().parent.parent
        / "src" / "codeanalyzer" / "report" / "template" / "compare_template.html"
    )
    text = template_path.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert scripts, "expected at least one inline <script> block"
    main_script = max(scripts, key=len)
    result = subprocess.run([node, "--check", "-"], input=main_script,
                             capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
