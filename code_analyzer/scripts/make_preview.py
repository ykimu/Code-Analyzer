#!/usr/bin/env python3
"""Regenerate preview_sample.html from the synthetic fixture used by
tests/test_report.py, without running the full pytest suite.

Usage:
    cd /home/claude/code_analyzer && PYTHONPATH=src python3 scripts/make_preview.py

This loads tests/test_report.py's build_sample_result() via a direct file
spec (rather than a package import) so it has no dependency on pytest's
collection/rootdir machinery.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

spec = importlib.util.spec_from_file_location("_test_report_fixture", REPO_ROOT / "tests" / "test_report.py")
test_report = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(test_report)

from codeanalyzer.report.html_builder import build_html  # noqa: E402


def main() -> None:
    result = test_report.build_sample_result()
    # Attach synthetic sources so the preview exercises the 影響範囲 tab's
    # interactive source viewer (range selection + dataflow highlighting),
    # not just the line-number-input fallback.
    result._sources = test_report.build_sample_sources()
    out_path = REPO_ROOT / "preview_sample.html"
    build_html(result, out_path)
    print(f"wrote {out_path} ({out_path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
