"""Common tree-sitter parsing infrastructure.

Each concrete parser consumes a ``ScannedFile`` and produces a
``ParseOutput`` carrying symbols, raw imports, calls, inheritance edges and
intra-function def-use records.  All line numbers are 1-indexed and inclusive
to match ``core.model.Span``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from tree_sitter import Language, Node, Parser

from codeanalyzer.core.model import DefUse, Span, Symbol, SymbolKind


class DefUseBuilder:
    """Accumulates def/use/return line info for one function symbol.

    Conservative by design: callers record rather than omit.  Lines are
    de-duplicated and sorted so output is deterministic.
    """

    def __init__(self, symbol_id: str) -> None:
        self.symbol_id = symbol_id
        self._defs: dict[str, set[int]] = {}
        self._uses: dict[str, set[int]] = {}
        self._returns: dict[str, set[int]] = {}
        self._param: dict[str, int] = {}

    def add_param(self, var: str, line: int, index: int) -> None:
        if not var:
            return
        self._param[var] = index
        self._defs.setdefault(var, set()).add(line)

    def add_def(self, var: str, line: int) -> None:
        if var:
            self._defs.setdefault(var, set()).add(line)

    def add_use(self, var: str, line: int) -> None:
        if var:
            self._uses.setdefault(var, set()).add(line)

    def add_return(self, var: str, line: int) -> None:
        if var:
            self._returns.setdefault(var, set()).add(line)
            self._uses.setdefault(var, set()).add(line)

    def build(self) -> list[DefUse]:
        out: list[DefUse] = []
        allvars = set(self._defs) | set(self._uses) | set(self._returns)
        for var in sorted(allvars):
            out.append(DefUse(
                symbol_id=self.symbol_id,
                var=var,
                def_lines=sorted(self._defs.get(var, ())),
                use_lines=sorted(self._uses.get(var, ())),
                return_lines=sorted(self._returns.get(var, ())),
                is_param=var in self._param,
                param_index=self._param.get(var, -1),
            ))
        return out

# Lazily-built (Language, Parser) caches keyed by a grammar name so we do not
# rebuild parsers per file.
_LANG_CACHE: dict[str, Language] = {}
_PARSER_CACHE: dict[str, Parser] = {}


def get_parser(grammar: str) -> Parser:
    """Return a cached tree-sitter ``Parser`` for a grammar key.

    ``grammar`` is one of: python, javascript, typescript, tsx, c, cpp, java.
    """
    if grammar in _PARSER_CACHE:
        return _PARSER_CACHE[grammar]
    lang = _load_language(grammar)
    parser = Parser(lang)
    _PARSER_CACHE[grammar] = parser
    return parser


def _load_language(grammar: str) -> Language:
    if grammar in _LANG_CACHE:
        return _LANG_CACHE[grammar]
    if grammar == "python":
        import tree_sitter_python as m
        lang = Language(m.language())
    elif grammar == "javascript":
        import tree_sitter_javascript as m
        lang = Language(m.language())
    elif grammar == "typescript":
        import tree_sitter_typescript as m
        lang = Language(m.language_typescript())
    elif grammar == "tsx":
        import tree_sitter_typescript as m
        lang = Language(m.language_tsx())
    elif grammar == "c":
        import tree_sitter_c as m
        lang = Language(m.language())
    elif grammar == "cpp":
        import tree_sitter_cpp as m
        lang = Language(m.language())
    elif grammar == "java":
        import tree_sitter_java as m
        lang = Language(m.language())
    else:  # pragma: no cover - defensive
        raise ValueError(f"unknown grammar {grammar!r}")
    _LANG_CACHE[grammar] = lang
    return lang


# ---------------------------------------------------------------------------
# Extraction payloads
# ---------------------------------------------------------------------------
@dataclass
class RawImport:
    """A single import/include reference exactly as written in source."""
    raw: str                       # module/path string as written
    line: int
    kind: str                      # import|from|require|dynamic|export_from|
                                   # include|java_import|package
    angle: bool = False            # C/C++ <...> include
    is_static: bool = False        # Java static import
    is_wildcard: bool = False      # Java/Python * import
    # (imported_name, local_name) pairs used to build call-resolution bindings.
    names: list[tuple[str, str]] = field(default_factory=list)
    # v1.6: True when a C/C++ #include sits inside a preprocessor conditional
    # region (#if/#ifdef/#ifndef/#elif/#else), excluding the common top-level
    # include-guard wrapper. Unused (stays False) for non-C/C++ imports.
    conditional: bool = False


@dataclass
class RawCall:
    caller_symbol_id: str
    callee: str
    receiver: Optional[str]
    line: int


@dataclass
class RawInherit:
    class_symbol_id: str
    base: str
    line: int


@dataclass
class RawVarType:
    """Raw, project-agnostic type facts for one variable/attribute in a scope.

    The parser records *names as written* (last dotted component); the resolver
    interprets them against the set of known project classes.  ``annos`` come
    from type annotations, ``ctors`` from ``x = Name(...)`` right-hand sides
    (the callee name), and ``other`` flags an assignment to anything that is not
    a simple call (which poisons constructor-based inference for that name).
    """
    annos: set[str] = field(default_factory=set)
    ctors: set[str] = field(default_factory=set)
    other: bool = False


@dataclass
class TypeHints:
    """Per-file lightweight symbol table consumed by type-aware resolution.

    * ``scope_vars``: scope symbol id -> {var name -> RawVarType} for function
      and module scopes (annotations + constructor assignments).
    * ``class_attrs``: class symbol id -> {attr name -> RawVarType} for
      ``self.attr`` annotations/constructor assignments.
    * ``returns``: function name -> set of return-annotation type names
      (module-local; used for ``g().method()`` and ``x = g()`` inference).
    """
    scope_vars: dict[str, dict[str, RawVarType]] = field(default_factory=dict)
    class_attrs: dict[str, dict[str, RawVarType]] = field(default_factory=dict)
    returns: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class ParseOutput:
    file_id: str
    symbols: list[Symbol] = field(default_factory=list)
    raw_imports: list[RawImport] = field(default_factory=list)
    calls: list[RawCall] = field(default_factory=list)
    inherits: list[RawInherit] = field(default_factory=list)
    defuses: list[DefUse] = field(default_factory=list)
    type_hints: TypeHints = field(default_factory=TypeHints)
    parse_ok: bool = True
    parse_partial: bool = False


# ---------------------------------------------------------------------------
# Node helpers
# ---------------------------------------------------------------------------
def text_of(node: Optional[Node]) -> str:
    if node is None:
        return ""
    return node.text.decode("utf-8", "replace")


def span_of(node: Node) -> Span:
    sr, sc = node.start_point
    er, ec = node.end_point
    return Span(sr + 1, er + 1, sc, ec)


def has_errors(root: Node) -> bool:
    """True if the tree contains any ERROR or MISSING node."""
    stack = [root]
    while stack:
        n = stack.pop()
        if n.is_error or n.is_missing:
            return True
        # has_error is a fast subtree flag; skip clean subtrees.
        if not n.has_error:
            continue
        stack.extend(n.children)
    return False


def iter_named(node: Node):
    """Yield all descendant nodes (pre-order), iterative."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        # reverse to preserve document order in the pre-order traversal
        stack.extend(reversed(n.children))


# ---------------------------------------------------------------------------
# Base parser
# ---------------------------------------------------------------------------
MODULE_SUFFIX = "::<module>"


class LanguageParser:
    """Abstract base.  Subclasses implement :meth:`parse`."""

    grammar = ""

    def __init__(self) -> None:
        self._used_ids: set[str] = set()

    # -- id management ----------------------------------------------------
    def module_id(self, file_id: str) -> str:
        return file_id + MODULE_SUFFIX

    def make_symbol_id(self, file_id: str, name_chain: list[str]) -> str:
        base = file_id + "".join("::" + n for n in name_chain)
        candidate = base
        n = 2
        while candidate in self._used_ids:
            candidate = f"{base}#{n}"
            n += 1
        self._used_ids.add(candidate)
        return candidate

    # -- entry point ------------------------------------------------------
    def parse(self, sf) -> ParseOutput:  # pragma: no cover - abstract
        raise NotImplementedError

    def _prepare(self, sf) -> tuple[Node, ParseOutput, Symbol]:
        """Common bootstrap: parse source, create MODULE symbol, detect errors."""
        self._used_ids = set()
        parser = get_parser(self.grammar)
        root = parser.parse(sf.source.encode("utf-8", "replace")).root_node
        out = ParseOutput(file_id=sf.id)
        partial = has_errors(root)
        out.parse_partial = partial
        out.parse_ok = not partial
        mod = Symbol(
            id=self.module_id(sf.id),
            name="<module>",
            kind=SymbolKind.MODULE.value,
            file_id=sf.id,
            span=span_of(root),
            parent_id=None,
            signature="",
        )
        self._used_ids.add(mod.id)
        out.symbols.append(mod)
        return root, out, mod
