"""Writes analysis.json — the on-disk, versioned JSON representation of an
AnalysisResult (see core/model.py for the schema contract).

Output is deterministic: AnalysisResult.to_dict() already returns
stably-sorted lists (by id / by (src,dst,kind,line) / etc.), and we dump
with sort_keys=False to preserve that intentional ordering (dict *key*
order for nested objects such as `meta` and `metrics` follows normal
Python dict insertion order, which is itself deterministic given a
deterministic AnalysisResult).
"""
from __future__ import annotations

import json
from pathlib import Path

from codeanalyzer.core.model import AnalysisResult


def write_json(result: AnalysisResult, out_path: Path, include_defuse: bool = False) -> None:
    """Serialize `result` to `out_path` as pretty-printed UTF-8 JSON.

    - ensure_ascii=False: keep Japanese/unicode text readable in the file.
    - indent=2: human-diffable output.
    - Deterministic: relies on AnalysisResult.to_dict()'s stable sorting of
      files/symbols/edges/diagnostics; no additional key sorting is applied
      so that logical field ordering (e.g. "id" before "path") is kept.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = result.to_dict(include_defuse=include_defuse)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    out_path.write_text(text + "\n", encoding="utf-8")
