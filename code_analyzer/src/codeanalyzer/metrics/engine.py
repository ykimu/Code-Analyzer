"""Software metrics engine (SPEC F5).

``compute_metrics(result, sources)`` fills ``result.metrics`` in place. It
never mutates ``result.files`` / ``result.symbols`` / ``result.edges`` --
those are the core worker's contract -- and it does not require re-running
the pipeline: it only needs the already-parsed ``AnalysisResult`` plus a
``{file_id: source_text}`` map (``pipeline.analyze_project(..., keep_sources=
True)`` attaches this as ``result._sources``; any equivalent mapping works,
e.g. one assembled by the caller from disk or from ``ScannedFile.source``).

Metrics shape (binding contract with the HTML report template -- key names
must match exactly, see report/template/report_template.html):

    metrics["files"][file_id] = {
        loc_total, loc_code, loc_comment, loc_blank,
        functions, classes, cc_total, cc_max, mi,
        fan_in, fan_out, fan_out_external,
    }
    metrics["symbols"][symbol_id] = {loc, cc, params}   # FUNCTION/METHOD only
    metrics["project"] = {
        files, loc_total, loc_code, loc_comment, functions, classes,
        cc_avg, cc_max, mi_avg, cycles_count,
        languages: {lang: {files, loc_code}},
        test: {files, loc_code},
    }
    metrics["cycles"] = [[file_id, ...], ...]   # reuses the pipeline's SCCs
    metrics["definitions"] = {metric_name: "short formula/definition"}
    metrics["test_files"] = [file_id, ...]      # sorted list (JSON-safe)

Design decisions / limitations (also surfaced via metrics["definitions"]
and duplicated here for maintainers):

* LOC is a statewful *line-based* scan (not a full lexer): it tracks
  block-comment state across lines and recognizes string literals (so a
  ``#`` or ``//`` inside a string does not get misread as a comment
  start), but it does not handle every language corner case (e.g. C
  trigraphs, nested template literals). Python docstrings are ordinary
  string-expression statements to the scanner, so they count as *code*
  lines, not comment lines -- this matches the SPEC F5 note that this
  decision should be documented in the report.
* A line containing both code and a comment (``x = 1  # note``) counts as
  a *code* line (SPEC instruction), never comment.
* CC (cyclomatic complexity) is computed per FUNCTION/METHOD symbol by
  re-parsing the file with the same tree-sitter grammar the core parsers
  use, matching each symbol to its defining AST node by (file_id,
  start_line, end_line) -- the parsers build symbol spans from that same
  node, so the match is exact in practice. CC = 1 + decision points in the
  function's own body, *excluding* nested function/method/class subtrees
  (those get their own CC when visited independently). Decision points:
  if/elif branches, for/while/do loops, switch/case labels (not
  ``default``), catch/except clauses, ternary/conditional expressions,
  ``&&``/``||`` (and/or) boolean operators, and Python comprehension
  ``if`` clauses. If a symbol's node cannot be located (e.g. the file
  failed to parse identically on re-parse), CC falls back to 1 and params
  to 0 for that symbol -- a documented limitation, not silently ignored
  (see ``metrics["definitions"]["cc"]``).
* MI uses the SEI formula with an *approximate* Halstead Volume: every
  non-comment tree-sitter leaf (terminal) token in the file is one
  Halstead "token"; N = total token count, n = count of distinct token
  texts. This is a simplification of the classical operator/operand split
  (SPEC explicitly allows an approximation). Empty files (loc_code == 0)
  are defined as MI = 100 (guards ``ln(0)``); ``ln(HV)`` is guarded to 0
  when HV <= 0.
* fan_in/fan_out are file-level, counted from IMPORT edges only, and
  de-duplicated per (src, dst) pair (multiple import lines to the same
  target file count once). Unresolved imports ("unresolved:...") are
  excluded entirely, per the task instruction. External imports
  ("external:<pkg>") are counted only in fan_out_external, and are
  distinct target packages, not import statement occurrences.
* Cross-language absolute comparison of these metrics is unreliable
  (different languages have very different token/line density); the
  report is expected to note this (SPEC F5).
"""
from __future__ import annotations

import math
from typing import Any

from codeanalyzer.core.model import AnalysisResult, Language, SymbolKind
from codeanalyzer.metrics.network import compute_network_metrics
from codeanalyzer.parsers.base import get_parser, text_of

# ---------------------------------------------------------------------------
# Language -> grammar plumbing
# ---------------------------------------------------------------------------


def _grammar_for(file_id: str, language: str) -> str:
    if language == Language.TYPESCRIPT.value:
        return "tsx" if file_id.lower().endswith(".tsx") else "typescript"
    if language == Language.JAVASCRIPT.value:
        return "javascript"
    if language == Language.C.value:
        return "c"
    if language == Language.CPP.value:
        return "cpp"
    if language == Language.JAVA.value:
        return "java"
    return "python"


# Node types that open a *new* CC/scope boundary per grammar (mirrors the
# core parsers' own _FUNC_TYPES/_CLASS_TYPES so CC counting nests exactly
# the way symbol extraction does).
_SCOPE_TYPES: dict[str, set[str]] = {
    "python": {"function_definition", "class_definition"},
    "javascript": {
        "function_declaration", "generator_function_declaration",
        "function_expression", "arrow_function", "method_definition",
        "function", "generator_function", "method_signature",
        "abstract_method_signature", "function_signature",
        "class_declaration", "class", "abstract_class_declaration",
        "interface_declaration",
    },
    "c": {"function_definition", "class_specifier", "struct_specifier"},
    "cpp": {"function_definition", "class_specifier", "struct_specifier"},
    "java": {
        "method_declaration", "constructor_declaration",
        "class_declaration", "interface_declaration", "enum_declaration",
        "record_declaration", "annotation_type_declaration",
    },
}
_SCOPE_TYPES["typescript"] = _SCOPE_TYPES["javascript"]
_SCOPE_TYPES["tsx"] = _SCOPE_TYPES["javascript"]

# Node types that define a FUNCTION/METHOD-like symbol (subset of
# _SCOPE_TYPES, excluding class/interface types) -- used to find candidate
# nodes to match against Symbol spans.
_FUNC_NODE_TYPES: dict[str, set[str]] = {
    "python": {"function_definition"},
    "javascript": {
        "function_declaration", "generator_function_declaration",
        "function_expression", "arrow_function", "method_definition",
        "function", "generator_function", "method_signature",
        "abstract_method_signature", "function_signature",
    },
    "c": {"function_definition"},
    "cpp": {"function_definition"},
    "java": {"method_declaration", "constructor_declaration"},
}
_FUNC_NODE_TYPES["typescript"] = _FUNC_NODE_TYPES["javascript"]
_FUNC_NODE_TYPES["tsx"] = _FUNC_NODE_TYPES["javascript"]


def _binary_op(node) -> str:
    op = node.child_by_field_name("operator")
    if op is not None:
        return text_of(op)
    for c in node.children:
        t = text_of(c)
        if t in ("&&", "||"):
            return t
    return ""


def _is_default_case(node) -> bool:
    # C/C++ case_statement: has a "value" field for `case X:`, none for
    # `default:`.
    return node.child_by_field_name("value") is None


def _is_default_label(node) -> bool:
    # Java switch_label: no stable field name across grammar versions;
    # check for a literal "default" child token instead.
    return any(text_of(c) == "default" for c in node.children)


def _is_decision(node, grammar: str) -> bool:
    t = node.type
    if grammar == "python":
        if t in ("if_statement", "elif_clause", "for_statement",
                  "while_statement", "except_clause", "conditional_expression",
                  "if_clause"):
            return True
        if t == "boolean_operator":
            return True
        return False
    if grammar in ("javascript", "typescript", "tsx"):
        if t in ("if_statement", "for_statement", "for_in_statement",
                  "for_of_statement", "while_statement", "do_statement",
                  "switch_case", "catch_clause", "ternary_expression"):
            return True
        if t == "binary_expression":
            return _binary_op(node) in ("&&", "||")
        return False
    if grammar in ("c", "cpp"):
        if t in ("if_statement", "for_statement", "while_statement",
                  "do_statement", "conditional_expression"):
            return True
        if t == "catch_clause":  # C++ only, harmless no-op for C
            return True
        if t == "case_statement":
            return not _is_default_case(node)
        if t == "binary_expression":
            return _binary_op(node) in ("&&", "||")
        return False
    if grammar == "java":
        if t in ("if_statement", "for_statement", "enhanced_for_statement",
                  "while_statement", "do_statement", "catch_clause",
                  "ternary_expression"):
            return True
        if t == "switch_label":
            return not _is_default_label(node)
        if t == "binary_expression":
            return _binary_op(node) in ("&&", "||")
        return False
    return False


def _cc_for_node(node, grammar: str) -> int:
    skip = _SCOPE_TYPES.get(grammar, ())
    count = 1
    stack = list(node.children)
    while stack:
        n = stack.pop()
        if n.type in skip:
            continue  # nested function/class gets its own CC
        if _is_decision(n, grammar):
            count += 1
        stack.extend(n.children)
    return count


def _count_params(func_node, grammar: str) -> int:
    if grammar == "python":
        params = func_node.child_by_field_name("parameters")
        if params is None:
            return 0
        return sum(1 for c in params.children if c.is_named)
    if grammar in ("javascript", "typescript", "tsx"):
        params = func_node.child_by_field_name("parameters")
        if params is None:
            return 0
        if params.type == "identifier":  # bare single-arg arrow: x => x
            return 1
        return sum(1 for c in params.children if c.is_named)
    if grammar in ("c", "cpp"):
        # descend through pointer/reference declarators to the
        # function_declarator, mirroring CCppParser._fdeclarator
        d = func_node.child_by_field_name("declarator")
        while d is not None and d.type in ("pointer_declarator",
                                            "reference_declarator"):
            d = d.child_by_field_name("declarator")
        if d is None or d.type != "function_declarator":
            return 0
        params = d.child_by_field_name("parameters")
        if params is None:
            return 0
        count = 0
        for c in params.children:
            if c.type == "parameter_declaration":
                if text_of(c).strip() == "void":
                    continue
                count += 1
            elif c.type == "variadic_parameter":
                count += 1
        return count
    if grammar == "java":
        params = func_node.child_by_field_name("parameters")
        if params is None:
            return 0
        return sum(1 for c in params.children
                   if c.type in ("formal_parameter", "spread_parameter"))
    return 0


def _leaf_tokens(root, ) -> list[str]:
    tokens: list[str] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.child_count == 0:
            if "comment" in n.type:
                continue
            txt = text_of(n)
            if txt.strip():
                tokens.append(txt)
            continue
        stack.extend(n.children)
    return tokens


# ---------------------------------------------------------------------------
# LOC (SPEC F5): physical / code / comment / blank
# ---------------------------------------------------------------------------

_LINE_COMMENT = {
    "python": "#",
}
_BLOCK_COMMENT = {
    # language -> (start, end); languages absent here have no block comment
}
_STRING_DELIMS: dict[str, tuple[str, ...]] = {
    "python": ('"""', "'''", '"', "'"),
    "javascript": ('"', "'", "`"),
    "typescript": ('"', "'", "`"),
    "c": ('"', "'"),
    "cpp": ('"', "'"),
    "java": ('"', "'"),
}


def _loc_config(language: str) -> tuple[str, tuple[str, str] | None, tuple[str, ...]]:
    if language == Language.PYTHON.value:
        return "#", None, _STRING_DELIMS["python"]
    return "//", ("/*", "*/"), _STRING_DELIMS.get(language, ('"', "'"))


def compute_loc(source: str, language: str) -> tuple[int, int, int, int]:
    """Return (loc_total, loc_code, loc_comment, loc_blank).

    Stateful line-based scan (see module docstring for the documented
    limitations). A line with both code and a comment counts as code.
    """
    if not source:
        return 0, 0, 0, 0

    line_comment, block_comment, string_delims = _loc_config(language)
    # physical line count: splitlines()-equivalent (no phantom trailing
    # blank line for a trailing "\n").
    raw_lines = source.split("\n")
    if raw_lines and raw_lines[-1] == "" and source.endswith("\n"):
        raw_lines = raw_lines[:-1]
    loc_total = len(raw_lines)
    if loc_total == 0:
        return 0, 0, 0, 0

    code_lines: set[int] = set()
    comment_lines: set[int] = set()

    n = len(source)
    i = 0
    line = 1
    in_string: str | None = None

    while i < n:
        c = source[i]
        if c == "\n":
            line += 1
            i += 1
            continue

        if in_string is not None:
            code_lines.add(line)
            if source.startswith(in_string, i):
                i += len(in_string)
                in_string = None
            elif in_string in ("'", '"', "`") and c == "\\" and i + 1 < n:
                i += 2
            else:
                i += 1
            continue

        matched = None
        for d in string_delims:
            if source.startswith(d, i):
                matched = d
                break
        if matched:
            in_string = matched
            code_lines.add(line)
            i += len(matched)
            continue

        if source.startswith(line_comment, i):
            comment_lines.add(line)
            j = source.find("\n", i)
            i = n if j == -1 else j
            continue

        if block_comment and source.startswith(block_comment[0], i):
            comment_lines.add(line)
            i += len(block_comment[0])
            while i < n and not source.startswith(block_comment[1], i):
                if source[i] == "\n":
                    line += 1
                    comment_lines.add(line)
                i += 1
            if i < n:
                i += len(block_comment[1])
            continue

        if not c.isspace():
            code_lines.add(line)
        i += 1

    code_lines = {l for l in code_lines if 1 <= l <= loc_total}
    comment_only = {l for l in comment_lines if 1 <= l <= loc_total} - code_lines
    loc_code = len(code_lines)
    loc_comment = len(comment_only)
    loc_blank = loc_total - loc_code - loc_comment
    return loc_total, loc_code, loc_comment, loc_blank


# ---------------------------------------------------------------------------
# MI (SPEC F5, SEI formula w/ approximated Halstead Volume)
# ---------------------------------------------------------------------------


def _halstead_volume(tokens: list[str]) -> float:
    N = len(tokens)
    n = len(set(tokens))
    if N == 0 or n <= 1:
        return 0.0
    return N * math.log2(n)


def _mi(cc_total: int, loc_code: int, hv: float) -> float:
    if loc_code <= 0:
        return 100.0
    ln_hv = math.log(hv) if hv > 0 else 0.0
    ln_loc = math.log(loc_code)
    raw = 171 - 5.2 * ln_hv - 0.23 * cc_total - 16.2 * ln_loc
    return max(0.0, min(100.0, raw * 100.0 / 171.0))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_metrics(result: AnalysisResult, sources: dict[str, str]) -> None:
    """Fill ``result.metrics`` in place per the shape documented above."""
    files_metrics: dict[str, dict[str, Any]] = {}
    symbols_metrics: dict[str, dict[str, Any]] = {}

    func_syms = [s for s in result.symbols
                if s.kind in (SymbolKind.FUNCTION.value, SymbolKind.METHOD.value)]
    class_syms = [s for s in result.symbols if s.kind == SymbolKind.CLASS.value]
    funcs_by_file: dict[str, list] = {}
    for s in func_syms:
        funcs_by_file.setdefault(s.file_id, []).append(s)
    classes_by_file: dict[str, list] = {}
    for s in class_syms:
        classes_by_file.setdefault(s.file_id, []).append(s)

    # -- fan-in / fan-out from IMPORT edges, deduped per (src, dst) --------
    internal_ids = {f.id for f in result.files}
    fan_out: dict[str, set[str]] = {}
    fan_out_ext: dict[str, set[str]] = {}
    fan_in: dict[str, set[str]] = {}
    for e in result.edges:
        if e.kind != "import":
            continue
        if e.dst.startswith("unresolved:"):
            continue
        if e.dst.startswith("external:"):
            fan_out_ext.setdefault(e.src, set()).add(e.dst)
            continue
        if e.dst in internal_ids:
            fan_out.setdefault(e.src, set()).add(e.dst)
            fan_in.setdefault(e.dst, set()).add(e.src)

    for f in result.files:
        source = sources.get(f.id, "")
        loc_total, loc_code, loc_comment, loc_blank = compute_loc(source, f.language)

        grammar = _grammar_for(f.id, f.language)
        cc_total = 0
        cc_max = 0
        node_by_span: dict[tuple[int, int], Any] = {}
        if source:
            try:
                root = get_parser(grammar).parse(
                    source.encode("utf-8", "replace")).root_node
            except Exception:  # noqa: BLE001 - metrics must never crash the run
                root = None
            if root is not None:
                func_types = _FUNC_NODE_TYPES.get(grammar, ())
                stack = [root]
                while stack:
                    n = stack.pop()
                    if n.type in func_types:
                        key = (n.start_point[0] + 1, n.end_point[0] + 1)
                        node_by_span.setdefault(key, n)
                    stack.extend(n.children)

                tokens = _leaf_tokens(root)
                hv = _halstead_volume(tokens)
            else:
                hv = 0.0
        else:
            hv = 0.0

        for sym in funcs_by_file.get(f.id, []):
            key = (sym.span.start_line, sym.span.end_line)
            node = node_by_span.get(key)
            if node is not None:
                cc = _cc_for_node(node, grammar)
                params = _count_params(node, grammar)
            else:
                # documented limitation: fall back to minimal complexity
                # when the symbol's span could not be matched to a
                # re-parsed AST node (e.g. partial/degraded reparse).
                cc = 1
                params = 0
            symbols_metrics[sym.id] = {"loc": sym.span.end_line - sym.span.start_line + 1,
                                       "cc": cc, "params": params}
            cc_total += cc
            cc_max = max(cc_max, cc)

        mi = _mi(cc_total, loc_code, hv)

        files_metrics[f.id] = {
            "loc_total": loc_total,
            "loc_code": loc_code,
            "loc_comment": loc_comment,
            "loc_blank": loc_blank,
            "functions": len(funcs_by_file.get(f.id, [])),
            "classes": len(classes_by_file.get(f.id, [])),
            "cc_total": cc_total,
            "cc_max": cc_max,
            "mi": mi,
            "fan_in": len(fan_in.get(f.id, ())),
            "fan_out": len(fan_out.get(f.id, ())),
            "fan_out_external": len(fan_out_ext.get(f.id, ())),
        }

    # -- cycles: reuse the pipeline's precomputed file-level SCCs ----------
    cycles = result.metrics.get("cycles")
    if cycles is None:
        from codeanalyzer.core.graph import build_graphs, tarjan_scc
        file_graph, _ = build_graphs(result.edges)
        cycles = tarjan_scc(file_graph, only_nodes=internal_ids)

    # -- project aggregate ---------------------------------------------------
    languages: dict[str, dict[str, int]] = {}
    test_ids: list[str] = []
    for f in result.files:
        fm = files_metrics[f.id]
        lg = languages.setdefault(f.language, {"files": 0, "loc_code": 0})
        lg["files"] += 1
        lg["loc_code"] += fm["loc_code"]
        if f.is_test:
            test_ids.append(f.id)
    test_ids.sort()

    all_cc = [symbols_metrics[s.id]["cc"] for s in func_syms]
    cc_avg = (sum(all_cc) / len(all_cc)) if all_cc else 0.0
    cc_max_proj = max(all_cc) if all_cc else 0
    mi_values = [files_metrics[f.id]["mi"] for f in result.files]
    mi_avg = (sum(mi_values) / len(mi_values)) if mi_values else 0.0

    test_loc_code = sum(files_metrics[fid]["loc_code"] for fid in test_ids)

    project = {
        "files": len(result.files),
        "loc_total": sum(m["loc_total"] for m in files_metrics.values()),
        "loc_code": sum(m["loc_code"] for m in files_metrics.values()),
        "loc_comment": sum(m["loc_comment"] for m in files_metrics.values()),
        "functions": len(func_syms),
        "classes": len(class_syms),
        "cc_avg": cc_avg,
        "cc_max": cc_max_proj,
        "mi_avg": mi_avg,
        "cycles_count": len(cycles),
        "languages": languages,
        "test": {"files": len(test_ids), "loc_code": test_loc_code},
    }

    definitions = {
        "loc_total": "Physical line count of the file (SPEC F5).",
        "loc_code": "Lines containing code (a line with both code and a "
                    "trailing comment counts as code, not comment). Python "
                    "docstrings are string-expression statements and count "
                    "as code, not comments.",
        "loc_comment": "Lines that are pure comment (# ; // ; /* ... */), "
                       "with no code on the same line.",
        "loc_blank": "loc_total - loc_code - loc_comment.",
        "functions": "Count of FUNCTION/METHOD symbols defined directly in "
                     "the file.",
        "classes": "Count of CLASS symbols defined directly in the file.",
        "cc": "Cyclomatic complexity = 1 + decision points in the "
              "symbol's own body (if/elif, for/while/do, switch/case "
              "(excl. default), catch/except, ternary, &&/|| (and/or), "
              "python comprehension `if`); nested function/class bodies "
              "are excluded and counted separately.",
        "cc_total": "Sum of cc over all functions/methods defined in the "
                    "file.",
        "cc_max": "Maximum cc over all functions/methods in the file (0 "
                  "if none).",
        "mi": "SEI Maintainability Index: max(0, min(100, (171 - "
              "5.2*ln(HV) - 0.23*CC_total - 16.2*ln(loc_code)) * 100/171)). "
              "HV (Halstead Volume) is approximated as N*log2(n) where N "
              "is the total count of non-comment tree-sitter leaf tokens "
              "in the file and n is the count of distinct token texts. "
              "Empty files (loc_code == 0) are defined as MI = 100.",
        "fan_in": "Count of distinct internal project files that import "
                  "this file.",
        "fan_out": "Count of distinct internal project files this file "
                   "imports.",
        "fan_out_external": "Count of distinct external:<package> import "
                            "targets from this file. Unresolved imports "
                            "are not counted in any fan metric.",
        "cycles": "File-level import strongly-connected components "
                  "(size > 1), via Tarjan's algorithm over IMPORT edges.",
        "cc_avg": "Project-wide mean cc across all functions/methods.",
        "mi_avg": "Project-wide mean of per-file mi (unweighted by LOC).",
        "test": "Files classified as tests (SPEC F5: test_*, *_test.*, "
               "*.spec.*, *.test.*, or under a tests/ or test/ directory) "
               "are aggregated separately via FileInfo.is_test; also "
               "listed individually in metrics['test_files'].",
        "cross_language_note": "Absolute comparison of these metrics "
                               "across languages is unreliable (token/line "
                               "density differs greatly); prefer relative "
                               "comparison within the same language.",
    }

    # -- v1.1 network metrics: merge per-file values, set network summary --
    network_per_file, network_summary = compute_network_metrics(result)
    for fid, nm in network_per_file.items():
        if fid in files_metrics:
            files_metrics[fid].update(nm)

    definitions.update({
        "pagerank": "PageRank (damping=0.85) via power iteration over the "
                    "internal file IMPORT graph (converges when the L1 "
                    "delta < 1e-10 or after 200 iterations); dangling nodes "
                    "(no outgoing edges) redistribute their rank uniformly. "
                    "Files with no import edges at all (isolated) are "
                    "defined as pagerank == 0.0 and excluded from the "
                    "iteration; the remaining files' ranks sum to 1, so the "
                    "values across all files still sum to 1.",
        "betweenness": "Shortest-path betweenness centrality (Brandes' "
                       "algorithm) on the directed file IMPORT graph, "
                       "normalized by (n-1)(n-2). 0.0 for all files when "
                       "n < 3.",
        "closeness": "Harmonic closeness computed over INCOMING import "
                     "paths: C(v) = (1/(n-1)) * sum_{u!=v} 1/d(u,v), where "
                     "d(u,v) is the directed shortest-path distance from u "
                     "to v. This measures how quickly other files reach v "
                     "by following import chains (v as import target), not "
                     "how far v's own imports fan out. 0.0 when n < 2.",
        "degree_centrality": "(fan_in + fan_out) / (n-1) on the "
                             "deduplicated, self-loop-free file IMPORT "
                             "graph. 0.0 when n < 2.",
        "density": "Directed graph density of the internal file IMPORT "
                   "graph: edges / (nodes * (nodes-1)). 0.0 when "
                   "nodes < 2.",
    })

    result.metrics["files"] = files_metrics
    result.metrics["symbols"] = symbols_metrics
    result.metrics["project"] = project
    result.metrics["cycles"] = cycles
    result.metrics["definitions"] = definitions
    result.metrics["test_files"] = test_ids
    result.metrics["network"] = network_summary
