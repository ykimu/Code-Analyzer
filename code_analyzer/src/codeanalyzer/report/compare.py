"""Compares two analysis.json payloads (as produced by
`AnalysisResult.to_dict()`) and produces a compare.json dict.

This module implements ONLY the transformation documented in the "v1.3
additions (contract)" block of core/model.py's module docstring — read
that docstring before touching this file, it is the binding shape. This
module is a pure, dependency-free (stdlib only) transformer: no
filesystem/git access happens here, `compare_analyses` only ever looks at
the two dicts it is handed.

Determinism: every list produced here is explicitly sorted (or built from
a fixed key order), so calling `compare_analyses` twice on the same inputs
yields dict-equal (and, since dict key insertion order is fixed by this
code, also `json.dumps`-equal) output.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

#: metrics.project numeric keys compared for project_delta, in the fixed
#: order they're considered (only keys present as numbers on BOTH sides are
#: ever emitted). The "languages" dict is explicitly excluded per contract.
_PROJECT_METRIC_KEYS = (
    "files", "loc_total", "loc_code", "loc_comment", "functions", "classes",
    "cc_avg", "cc_max", "mi_avg", "cycles_count",
)

#: metrics.files[fid] numeric keys compared for files.changed, in a fixed
#: order for deterministic output. Any additional "loc_*" key present on
#: both sides but not listed here is appended (sorted) at diff time, which
#: keeps this forward-compatible with new loc_* breakdowns without silently
#: widening the "diff everything numeric" surface — e.g. fan_out_external
#: is deliberately NOT compared because it is not in this list and does not
#: match the "loc_*" prefix.
_KNOWN_FILE_METRIC_KEYS = (
    "loc_total", "loc_code", "loc_comment", "loc_blank",
    "functions", "classes", "cc_total", "cc_max", "mi",
    "fan_in", "fan_out", "pagerank", "betweenness", "closeness",
    "degree_centrality", "churn_commits", "hotspot",
)

_EPS = 1e-9

_CMP_TOKEN = "__CA_CMP_JSON__"
_TEMPLATE_DIR = Path(__file__).resolve().parent / "template"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _changed(old_val: Any, new_val: Any) -> bool:
    return abs(float(old_val) - float(new_val)) > _EPS


def _validate(doc: Any, label: str) -> None:
    if not isinstance(doc, dict):
        raise ValueError(f"{label} analysis must be a dict, got {type(doc).__name__}")
    for key in ("schema_version", "files", "metrics"):
        if key not in doc:
            raise ValueError(f"{label} analysis is missing required key {key!r}")


def _file_metric_keys_for(old_row: dict, new_row: dict) -> list[str]:
    keys = [k for k in _KNOWN_FILE_METRIC_KEYS if k in old_row and k in new_row]
    extra_loc = sorted(
        k for k in (set(old_row) & set(new_row))
        if k.startswith("loc_") and k not in _KNOWN_FILE_METRIC_KEYS
    )
    return keys + extra_loc


def _project_delta(old_proj: dict, new_proj: dict) -> dict:
    delta: dict = {}
    for key in _PROJECT_METRIC_KEYS:
        if key not in old_proj or key not in new_proj:
            continue
        old_val, new_val = old_proj[key], new_proj[key]
        if not (_is_number(old_val) and _is_number(new_val)):
            continue
        if _changed(old_val, new_val):
            delta[key] = [old_val, new_val]
    return delta


def _files_diff(old: dict, new: dict) -> dict:
    old_ids = {f["id"] for f in (old.get("files") or [])}
    new_ids = {f["id"] for f in (new.get("files") or [])}
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)

    old_metrics_files = (old.get("metrics") or {}).get("files") or {}
    new_metrics_files = (new.get("metrics") or {}).get("files") or {}
    common_ids = sorted(set(old_metrics_files) & set(new_metrics_files))

    changed = []
    for fid in common_ids:
        old_row = old_metrics_files[fid] or {}
        new_row = new_metrics_files[fid] or {}
        deltas: dict = {}
        for key in _file_metric_keys_for(old_row, new_row):
            old_val, new_val = old_row[key], new_row[key]
            if not (_is_number(old_val) and _is_number(new_val)):
                continue
            if _changed(old_val, new_val):
                deltas[key] = [old_val, new_val]
        if deltas:
            changed.append({"file_id": fid, "deltas": deltas})

    return {"added": added, "removed": removed, "changed": changed}


def _import_edge_set(doc: dict) -> set[tuple[str, str, str]]:
    edges = doc.get("edges") or []
    return {
        (e["src"], e["dst"], e["kind"])
        for e in edges
        if e.get("kind") == "import"
    }


def _edge_obj(t: tuple[str, str, str]) -> dict:
    return {"src": t[0], "dst": t[1], "kind": t[2]}


def _edges_diff(old: dict, new: dict) -> dict:
    old_set = _import_edge_set(old)
    new_set = _import_edge_set(new)
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    return {
        "added": [_edge_obj(t) for t in added],
        "removed": [_edge_obj(t) for t in removed],
    }


def _cycle_sets(doc: dict) -> set:
    cycles = (doc.get("metrics") or {}).get("cycles") or []
    return {frozenset(c) for c in cycles}


def _cycles_diff(old: dict, new: dict) -> dict:
    old_sets = _cycle_sets(old)
    new_sets = _cycle_sets(new)
    added = sorted(sorted(fs) for fs in (new_sets - old_sets))
    removed = sorted(sorted(fs) for fs in (old_sets - new_sets))
    return {"added": added, "removed": removed}


def _hotspots_diff(old: dict, new: dict) -> Optional[dict]:
    old_hot = (old.get("metrics") or {}).get("hotspots") or []
    new_hot = (new.get("metrics") or {}).get("hotspots") or []
    if not old_hot or not new_hot:
        return None

    def sort_key(entry: dict) -> tuple:
        return (-float(entry.get("hotspot", 0.0)), str(entry.get("file_id", "")))

    old_by_id = {e["file_id"]: e for e in old_hot}
    new_by_id = {e["file_id"]: e for e in new_hot}

    new_ids = set(new_by_id) - set(old_by_id)
    resolved_ids = set(old_by_id) - set(new_by_id)

    new_list = sorted((new_by_id[i] for i in new_ids), key=sort_key)
    resolved_list = sorted((old_by_id[i] for i in resolved_ids), key=sort_key)
    return {"new": new_list, "resolved": resolved_list}


def compare_analyses(old: dict, new: dict) -> dict:
    """Diff two AnalysisResult.to_dict() payloads into a compare.json dict.

    See core/model.py's "v1.3 additions (contract)" docstring block for the
    exact output shape. Raises ValueError if either input is missing
    "schema_version", "files", or "metrics".
    """
    _validate(old, "old")
    _validate(new, "new")

    result: dict = {
        "schema_version": new["schema_version"],
        "kind": "comparison",
        "old_meta": dict(old.get("meta") or {}),
        "new_meta": dict(new.get("meta") or {}),
        "project_delta": _project_delta(
            (old.get("metrics") or {}).get("project") or {},
            (new.get("metrics") or {}).get("project") or {},
        ),
        "files": _files_diff(old, new),
        "edges": _edges_diff(old, new),
        "cycles": _cycles_diff(old, new),
    }
    hotspots = _hotspots_diff(old, new)
    if hotspots is not None:
        result["hotspots"] = hotspots
    return result


def _read_compare_template() -> str:
    template_path = _TEMPLATE_DIR / "compare_template.html"
    return template_path.read_text(encoding="utf-8")


def _encode_compare_json(cmp: dict) -> str:
    """Plain (non-gzipped) JSON payload, safe to embed inside a
    `<script>` element: `</` sequences are escaped to `<\\/` so a literal
    "</script>" occurring inside a string value (e.g. a file path) cannot
    prematurely terminate the embedding script tag.
    """
    text = json.dumps(cmp, ensure_ascii=False)
    return text.replace("</", "<\\/")


#: Second placeholder: the literal Japanese "no differences" headline is
#: injected server-side (not left to client-side JS) so a comparison with
#: no detectable differences is unambiguous straight from the HTML source
#: -- and, symmetrically, a non-empty comparison's HTML never contains the
#: phrase at all.
_EMPTY_TEXT_TOKEN = "__CA_EMPTY_TEXT__"
_EMPTY_TEXT = "差分はありません"


def _is_empty_compare(cmp: dict) -> bool:
    """True when `cmp` (a dict shaped like compare_analyses()'s return
    value) carries no detectable differences in any section."""
    if cmp.get("project_delta"):
        return False
    files = cmp.get("files") or {}
    if files.get("added") or files.get("removed") or files.get("changed"):
        return False
    edges = cmp.get("edges") or {}
    if edges.get("added") or edges.get("removed"):
        return False
    cycles = cmp.get("cycles") or {}
    if cycles.get("added") or cycles.get("removed"):
        return False
    hotspots = cmp.get("hotspots")
    if hotspots and (hotspots.get("new") or hotspots.get("resolved")):
        return False
    return True


def build_compare_html(cmp: dict, out_path: Path) -> None:
    """Render the self-contained comparison HTML report for `cmp` (the dict
    returned by `compare_analyses`) to `out_path`.

    No templating engine is used for the same reason as html_builder.py:
    plain str.replace on unique placeholder tokens avoids double-escaping
    risk from "{", "}", "$" characters that could legitimately appear in
    file paths embedded in the JSON payload.

    Whether `cmp` carries any differences is decided here (server-side),
    not solely deferred to client-side JS: the "差分はありません" headline
    is only ever written into the output when `cmp` is genuinely empty, so
    the produced HTML is unambiguous even before any script runs.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    template = _read_compare_template()
    if _CMP_TOKEN not in template:
        raise ValueError(f"compare_template.html is missing the {_CMP_TOKEN!r} placeholder")
    if _EMPTY_TEXT_TOKEN not in template:
        raise ValueError(f"compare_template.html is missing the {_EMPTY_TEXT_TOKEN!r} placeholder")

    payload = _encode_compare_json(cmp)
    empty_text = _EMPTY_TEXT if _is_empty_compare(cmp) else ""

    html = template.replace(_CMP_TOKEN, payload, 1)
    html = html.replace(_EMPTY_TEXT_TOKEN, empty_text, 1)

    out_path.write_text(html, encoding="utf-8")
