"""Language parsers and dispatch."""
from __future__ import annotations

from codeanalyzer.core.model import Diagnostic, Language
from codeanalyzer.parsers.base import ParseOutput
from codeanalyzer.parsers.c_cpp_parser import CCppParser
from codeanalyzer.parsers.java_parser import JavaParser
from codeanalyzer.parsers.js_ts_parser import JsTsParser
from codeanalyzer.parsers.python_parser import PythonParser


def parse_file(sf) -> tuple[ParseOutput, list[Diagnostic]]:
    """Parse one ScannedFile; never raises. Returns (output, diagnostics)."""
    diags: list[Diagnostic] = []
    try:
        if sf.language == Language.PYTHON:
            parser = PythonParser()
        elif sf.language in (Language.JAVASCRIPT, Language.TYPESCRIPT):
            parser = JsTsParser.for_file(sf)
        elif sf.language in (Language.C, Language.CPP):
            parser = CCppParser.for_file(sf)
        elif sf.language == Language.JAVA:
            parser = JavaParser()
        else:  # pragma: no cover
            raise ValueError(f"no parser for {sf.language}")
        out = parser.parse(sf)
    except Exception as e:  # noqa: BLE001 - robustness: isolate failures
        diags.append(Diagnostic(sf.id, "parse_error",
                                f"{type(e).__name__}: {e}"))
        out = ParseOutput(file_id=sf.id, parse_ok=False, parse_partial=False)
        return out, diags

    if not out.parse_ok:
        diags.append(Diagnostic(sf.id, "parse_partial",
                                "tree-sitter reported ERROR nodes"))
    return out, diags


__all__ = [
    "parse_file", "PythonParser", "JsTsParser", "CCppParser", "JavaParser",
]
