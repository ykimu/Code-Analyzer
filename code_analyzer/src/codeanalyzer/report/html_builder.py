"""Builds the single self-contained HTML report (report.html).

Takes the report_template.html + vendored d3.v7.min.js shipped alongside
this module, embeds the analysis data (gzip+base64 of AnalysisResult's
JSON), and writes the final offline HTML file.

No templating engine is used (no str.format / string.Template) because the
d3 source and the JSON payload can legitimately contain "{", "}", "$" and
other characters that those engines treat specially, which would corrupt
the output. Plain str.replace on two unique, unambiguous placeholder
tokens avoids any double-escaping risk.
"""
from __future__ import annotations

import base64
import gzip
import json
import sys
from pathlib import Path

from codeanalyzer.core.model import AnalysisResult

#: Directory containing report_template.html and d3.v7.min.js (this package's
#: `template/` subdirectory, shipped as package data).
_TEMPLATE_DIR = Path(__file__).resolve().parent / "template"

_D3_TOKEN = "/*__D3_JS__*/"
_DATA_TOKEN = "__CA_DATA_B64_GZIP__"

#: SPEC section 3: "HTMLサイズ: 目安50MB以下" (roughly 50MB, or warn).
_SIZE_WARN_BYTES = 50 * 1024 * 1024

#: v1.1 source-embedding guardrails (影響範囲 tab source viewer):
#: skip any single file whose source text exceeds this many characters...
_MAX_SOURCE_FILE_CHARS = 200_000
#: ...and stop embedding further sources once the running total exceeds this.
_MAX_TOTAL_SOURCE_CHARS = 20_000_000


def _read_template() -> str:
    template_path = _TEMPLATE_DIR / "report_template.html"
    return template_path.read_text(encoding="utf-8")


def _read_d3() -> str:
    d3_path = _TEMPLATE_DIR / "d3.v7.min.js"
    return d3_path.read_text(encoding="utf-8")


def _filter_sources(sources: dict) -> tuple[dict, list]:
    """Apply the per-file and cumulative size guardrails to `sources`.

    Returns (filtered_sources, omitted_ids). Iterates in sorted id order for
    determinism (matches AnalysisResult.to_dict()'s own sorting of sources).
    A file over the per-file cap is skipped (and does not count toward the
    running total). Once the running total of *included* file sizes exceeds
    the cumulative cap, that file and every remaining file are omitted.
    """
    filtered: dict = {}
    omitted: list = []
    total = 0
    stopped = False
    for fid in sorted(sources.keys()):
        if stopped:
            omitted.append(fid)
            continue
        text = sources[fid]
        if len(text) > _MAX_SOURCE_FILE_CHARS:
            omitted.append(fid)
            continue
        total += len(text)
        if total > _MAX_TOTAL_SOURCE_CHARS:
            omitted.append(fid)
            stopped = True
            continue
        filtered[fid] = text
    return filtered, omitted


def _encode_data(result: AnalysisResult, embed_sources: bool = True) -> str:
    """JSON -> UTF-8 bytes -> gzip (level 9) -> base64 (ASCII str)."""
    raw_sources = getattr(result, "_sources", None) if embed_sources else None
    if isinstance(raw_sources, dict):
        filtered, omitted = _filter_sources(raw_sources)
        data = result.to_dict(include_defuse=True, sources=filtered)
        if omitted:
            data["sources_omitted"] = sorted(omitted)
    else:
        data = result.to_dict(include_defuse=False)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    return base64.b64encode(compressed).decode("ascii")


def build_html(result: AnalysisResult, out_path: Path, embed_sources: bool = True) -> None:
    """Render the self-contained HTML report for `result` to `out_path`.

    When `embed_sources` is true and `result._sources` (a {file_id: source
    text} dict, set by the CLI when --embed-sources is used) is present,
    the sources (and defuses, for client-side slicing) are embedded into
    the payload so the 影響範囲 tab can render an interactive source viewer.
    Oversized files/payloads are dropped per `_filter_sources` and recorded
    under the "sources_omitted" key. Backward compatible: positional-arg
    callers that omit `embed_sources` keep the previous embedding behavior
    automatically (sources are embedded only if `_sources` is actually set).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    template = _read_template()
    d3_source = _read_d3()
    b64_payload = _encode_data(result, embed_sources)

    if _D3_TOKEN not in template:
        raise ValueError(f"report_template.html is missing the {_D3_TOKEN!r} placeholder")
    if _DATA_TOKEN not in template:
        raise ValueError(f"report_template.html is missing the {_DATA_TOKEN!r} placeholder")

    html = template.replace(_D3_TOKEN, d3_source, 1)
    html = html.replace(_DATA_TOKEN, b64_payload, 1)

    out_path.write_text(html, encoding="utf-8")

    size = out_path.stat().st_size
    if size > _SIZE_WARN_BYTES:
        print(
            f"warning: report.html is {size / (1024 * 1024):.1f}MB, "
            f"exceeding the {_SIZE_WARN_BYTES / (1024 * 1024):.0f}MB guideline "
            "(SPEC section 3). Consider re-running with symbol-edge thinning.",
            file=sys.stderr,
        )
