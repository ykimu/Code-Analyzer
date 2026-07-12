"""Core data model (IR) shared by all analyzer components.

This module is the CONTRACT between the parsing core, metrics engine,
impact analyzer, and report builders. Do not change field names without
updating SCHEMA_VERSION and every consumer.

JSON schema (analysis.json, embedded into the HTML report):
{
  "schema_version": "1.0",
  "meta": {tool_version, root, generated_at, git_revision, excludes,
           includes, gitignore_respected, file_count, parse_failures,
           unresolved_imports, languages: {lang: file_count}},
  "files": [FileInfo...],
  "symbols": [Symbol...],
  "edges": [Edge...],
  "metrics": {"project": {...}, "files": {file_id: {...}},
              "symbols": {symbol_id: {...}}, "cycles": [[file_id,...],...],
              "network": {...}},          # v1.1: graph-level network stats
  "diagnostics": [Diagnostic...],
  "impact": ImpactResult | null,
  "sources": {file_id: source_text},      # v1.1: present only when embedded (--embed-sources)
  "defuses": [DefUse...]                  # v1.1: embedded alongside sources for client-side slicing
}

v1.1 metrics additions (contract):
- metrics["files"][fid] gains: "pagerank" (damping 0.85, normalized, 6dp),
  "betweenness" (Brandes, normalized by (n-1)(n-2), 6dp),
  "closeness" (harmonic closeness on the directed import graph, normalized, 6dp),
  "degree_centrality" ((fan_in+fan_out)/(n-1), 6dp).
  All computed on the internal file-level IMPORT graph; 0.0 for isolated nodes.
- metrics["network"] = {"nodes", "edges", "density", "avg_degree",
  "weakly_connected_components", "largest_component_size"}
- metrics["definitions"] documents each new metric.

v1.3 additions (contract, schema 1.2 / tool 1.3.0 — constants bumped in the
integration phase, engines must code against these shapes now):
- metrics["files"][fid] gains (only when the project is a git repo):
  "churn_commits" (int, commits touching the file within the window),
  "churn_added"/"churn_deleted" (int, lines from --numstat),
  "last_commit" (ISO date str), and
  "hotspot" (float 0-1, 4dp: percentile_rank(churn_commits) *
             percentile_rank(cc_total); 0.0 when either is 0 or no git).
- metrics["churn"] = {"available": bool, "window_days": int|null,
  "commits_scanned": int, "reason": str|null (why unavailable)}
- metrics["hotspots"] = top-20 list sorted by hotspot desc, then file_id,
  containing ONLY entries with hotspot > 0 (may be shorter than 20 or empty):
  [{"file_id", "hotspot", "churn_commits", "cc_total", "loc_code"}]
- window_days semantics: positive int = --since window; None = full history;
  0 = churn collection DISABLED (callers must not invoke collect_churn).
- Impact result for diff mode (analyze_impact_diff): same top-level shape as
  ImpactResult plus "query": {..., "diff": "<revspec or 'worktree'>"},
  "hunks": [{"file": file_id, "start_line": int, "end_line": int,
             "status": "analyzed"|"skipped", "reason": str|null}],
  "origins": [per-hunk origin objects]; "origin" stays = origins[0] for
  backward compatibility. affected_files merged across hunks (union lines,
  min hops, strongest confidence).
- compare.json (report/compare.py):
  {"schema_version", "kind": "comparison",
   "old_meta": {...}, "new_meta": {...},
   "project_delta": {metric: [old, new]},
   "files": {"added": [ids], "removed": [ids],
             "changed": [{"file_id", "deltas": {metric: [old, new]}}]},
   "edges": {"added": [{src,dst,kind}], "removed": [...]}  (IMPORT edges,
             dedup by (src,dst,kind), line ignored),
   "cycles": {"added": [[ids]], "removed": [[ids]]},
   "hotspots": {"new": [...], "resolved": [...]} (present when both sides
             carry hotspot data)}
  All lists stable-sorted; numeric deltas only listed when |old-new| > 1e-9.
"""
from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any, Optional

SCHEMA_VERSION = "1.2"
TOOL_VERSION = "1.5.0"


class Language(str, enum.Enum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    C = "c"
    CPP = "cpp"
    JAVA = "java"


#: file extension -> Language
EXTENSION_MAP: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".js": Language.JAVASCRIPT, ".jsx": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT, ".cjs": Language.JAVASCRIPT,
    ".ts": Language.TYPESCRIPT, ".tsx": Language.TYPESCRIPT,
    ".c": Language.C, ".h": Language.C,
    ".cpp": Language.CPP, ".cc": Language.CPP, ".cxx": Language.CPP,
    ".hpp": Language.CPP, ".hh": Language.CPP,
    ".java": Language.JAVA,
}


class SymbolKind(str, enum.Enum):
    FUNCTION = "function"
    METHOD = "method"
    CLASS = "class"
    VARIABLE = "variable"        # module/file-level variable definition
    MODULE = "module"            # synthetic symbol representing a whole file


class EdgeKind(str, enum.Enum):
    IMPORT = "import"            # file -> file (or file -> external package)
    CALL = "call"                # symbol -> symbol
    INHERIT = "inherit"          # class -> class
    TYPE_REF = "type_ref"        # symbol -> class/type
    DATAFLOW = "dataflow"        # def-use propagation edge (impact analysis)


class Confidence(str, enum.Enum):
    CERTAIN = "certain"          # syntactically unambiguous
    INFERRED = "inferred"        # name-based best-effort resolution
    UNRESOLVED = "unresolved"    # target not found in project


@dataclass
class Span:
    """1-indexed, inclusive line range."""
    start_line: int
    end_line: int
    start_col: int = 0
    end_col: int = 0

    def contains_line(self, line: int) -> bool:
        return self.start_line <= line <= self.end_line


@dataclass
class FileInfo:
    id: str                      # stable id: relative posix path from root
    path: str                    # same as id (relative posix path)
    language: str                # Language value
    size_bytes: int = 0
    is_test: bool = False
    parse_ok: bool = True
    parse_partial: bool = False  # tree-sitter reported ERROR nodes


@dataclass
class Symbol:
    id: str                      # FQN: "<file path>::<Class>::<name>" (unique; suffix "#N" on collision)
    name: str
    kind: str                    # SymbolKind value
    file_id: str
    span: Span
    parent_id: Optional[str] = None   # enclosing class/function symbol id
    signature: str = ""               # best-effort textual signature


@dataclass
class Edge:
    src: str                     # file id (IMPORT) or symbol id (others)
    dst: str                     # resolved file/symbol id, or "external:<pkg>" or "unresolved:<name>"
    kind: str                    # EdgeKind value
    confidence: str = Confidence.CERTAIN.value
    ambiguous: bool = False      # dst is one of several same-name candidates
    line: int = 0                # source line of the reference

    def key(self) -> tuple:
        return (self.src, self.dst, self.kind, self.line)


@dataclass
class DefUse:
    """Intra-function def-use record used by the impact analyzer.

    Produced by parsers per function symbol: which variables are
    defined/assigned at which lines, and used at which lines. Parameters
    are defs at the function's start line; return statements record the
    variables they use (use_kind='return').
    """
    symbol_id: str               # enclosing function/method symbol
    var: str
    def_lines: list[int] = field(default_factory=list)
    use_lines: list[int] = field(default_factory=list)
    return_lines: list[int] = field(default_factory=list)  # lines where var flows into a return
    is_param: bool = False
    param_index: int = -1        # 0-based position if is_param


@dataclass
class Diagnostic:
    file: str
    kind: str    # "parse_error" | "parse_partial" | "skipped_size" | "encoding_fallback" | "unresolved_import"
    message: str
    line: int = 0


@dataclass
class Meta:
    tool_version: str = TOOL_VERSION
    root: str = ""
    generated_at: str = ""       # ISO timestamp, injected by CLI
    git_revision: str = ""
    excludes: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    gitignore_respected: bool = True
    file_count: int = 0
    parse_failures: int = 0
    unresolved_imports: int = 0
    languages: dict[str, int] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    """Top-level container. `metrics` and `impact` are filled by their engines."""
    meta: Meta = field(default_factory=Meta)
    files: list[FileInfo] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    defuses: list[DefUse] = field(default_factory=list)   # NOT serialized to HTML (JSON only, optional)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    impact: Optional[dict[str, Any]] = None

    # ---- lookup helpers -------------------------------------------------
    def file_by_id(self, fid: str) -> Optional[FileInfo]:
        return next((f for f in self.files if f.id == fid), None)

    def symbols_in_file(self, fid: str) -> list[Symbol]:
        return [s for s in self.symbols if s.file_id == fid]

    def symbol_at(self, fid: str, line: int) -> Optional[Symbol]:
        """Smallest (innermost) non-module symbol containing the line."""
        cands = [s for s in self.symbols
                 if s.file_id == fid and s.kind != SymbolKind.MODULE.value
                 and s.span.contains_line(line)]
        if not cands:
            return None
        return min(cands, key=lambda s: s.span.end_line - s.span.start_line)

    # ---- serialization --------------------------------------------------
    def to_dict(self, include_defuse: bool = False,
                sources: Optional[dict[str, str]] = None) -> dict[str, Any]:
        d = {
            "schema_version": SCHEMA_VERSION,
            "meta": dataclasses.asdict(self.meta),
            "files": sorted((dataclasses.asdict(f) for f in self.files),
                            key=lambda x: x["id"]),
            "symbols": sorted((dataclasses.asdict(s) for s in self.symbols),
                              key=lambda x: x["id"]),
            "edges": sorted((dataclasses.asdict(e) for e in self.edges),
                            key=lambda x: (x["src"], x["dst"], x["kind"], x["line"])),
            "metrics": self.metrics,
            "diagnostics": sorted((dataclasses.asdict(x) for x in self.diagnostics),
                                  key=lambda x: (x["file"], x["kind"], x["line"])),
            "impact": self.impact,
        }
        if include_defuse:
            d["defuses"] = sorted((dataclasses.asdict(u) for u in self.defuses),
                                  key=lambda x: (x["symbol_id"], x["var"]))
        if sources is not None:
            d["sources"] = dict(sorted(sources.items()))
        return d


# ---------------------------------------------------------------------------
# Impact result shape (dict, produced by impact/analyzer.py):
# {
#   "query": {"file": str, "start_line": int, "end_line": int, "depth": int},
#   "origin": {"symbol_id": str, "symbol_name": str, "kind": str},
#   "affected_files": [
#       {"file_id": str,
#        "symbols": [{"symbol_id": str, "lines": [int,...],
#                     "confidence": "certain"|"inferred",
#                     "via": "call"|"import"|"dataflow",
#                     "hops": int}]}
#   ],
#   "sliced_vars": [str, ...],          # variables traced by the forward slice
#   "unresolved_boundaries": [
#       {"symbol_id": str, "reason": str, "line": int}
#   ],
#   "stats": {"files": int, "symbols": int, "max_hops_reached": bool}
# }
# ---------------------------------------------------------------------------
