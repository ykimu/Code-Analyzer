"""JavaScript / TypeScript / TSX parser."""
from __future__ import annotations

from tree_sitter import Node

from codeanalyzer.core.model import Language, Symbol, SymbolKind
from codeanalyzer.parsers.base import (
    DefUseBuilder,
    LanguageParser,
    ParseOutput,
    RawCall,
    RawImport,
    RawInherit,
    span_of,
    text_of,
)

_FUNC_TYPES = {
    "function_declaration", "generator_function_declaration",
    "function_expression", "arrow_function", "method_definition",
    "function", "generator_function", "method_signature",
    "abstract_method_signature", "function_signature",
}
_CLASS_TYPES = {
    "class_declaration", "class", "abstract_class_declaration",
    "interface_declaration",
}
_SCOPE_TYPES = _FUNC_TYPES | _CLASS_TYPES


def _strip_str(node: Node) -> str:
    txt = text_of(node).strip()
    if len(txt) >= 2 and txt[0] in "\"'`":
        return txt[1:-1]
    return txt


class JsTsParser(LanguageParser):
    def __init__(self, grammar: str = "javascript") -> None:
        super().__init__()
        self.grammar = grammar

    @classmethod
    def for_file(cls, sf) -> "JsTsParser":
        if sf.language == Language.JAVASCRIPT:
            return cls("javascript")
        if str(sf.path).lower().endswith(".tsx"):
            return cls("tsx")
        return cls("typescript")

    def parse(self, sf) -> ParseOutput:
        root, out, mod = self._prepare(sf)
        func_nodes: list[tuple[str, Node]] = []

        # (node, chain, parent_sym_id, call_owner, parent_is_class)
        stack = [(root, (), mod.id, mod.id, False)]
        while stack:
            node, chain, parent_id, owner, in_class = stack.pop()
            t = node.type

            if t in _CLASS_TYPES:
                name = text_of(node.child_by_field_name("name")) or \
                    f"<anon@L{node.start_point[0] + 1}>"
                new_chain = chain + (name,)
                sym = self._make(sf.id, new_chain, name, node,
                                 SymbolKind.CLASS, parent_id, f"class {name}")
                out.symbols.append(sym)
                self._inherits(node, sym.id, out)
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append((body, new_chain, sym.id, owner, True))
                continue

            if t in _FUNC_TYPES:
                name = self._func_name(node)
                kind = SymbolKind.METHOD if in_class else SymbolKind.FUNCTION
                new_chain = chain + (name,)
                sym = self._make(sf.id, new_chain, name, node, kind,
                                 parent_id, self._signature(node, name))
                out.symbols.append(sym)
                func_nodes.append((sym.id, node))
                body = node.child_by_field_name("body")
                if body is not None:
                    stack.append((body, new_chain, sym.id, sym.id, False))
                # arrow bodies may be an expression, not a statement_block
                continue

            if t == "import_statement":
                self._es_import(node, out)
                continue
            if t == "export_statement":
                self._export_from(node, out)
            if t == "call_expression":
                self._call(node, owner, out)
            if t == "new_expression":
                self._new(node, owner, out)

            for c in reversed(node.children):
                stack.append((c, chain, parent_id, owner, in_class))

        for sym_id, node in func_nodes:
            self._defuse(sym_id, node, out)

        out.symbols.sort(key=lambda s: (s.span.start_line, s.id))
        return out

    # ------------------------------------------------------------------
    def _make(self, file_id, chain, name, node, kind, parent_id, sig) -> Symbol:
        return Symbol(
            id=self.make_symbol_id(file_id, list(chain)),
            name=name, kind=kind.value, file_id=file_id,
            span=span_of(node), parent_id=parent_id, signature=sig,
        )

    def _func_name(self, node: Node) -> str:
        nm = node.child_by_field_name("name")
        if nm is not None:
            return text_of(nm)
        p = node.parent
        if p is not None:
            if p.type == "variable_declarator":
                return text_of(p.child_by_field_name("name")) or \
                    self._anon(node)
            if p.type == "pair":
                return text_of(p.child_by_field_name("key")) or self._anon(node)
            if p.type in ("assignment_expression",):
                return text_of(p.child_by_field_name("left")) or \
                    self._anon(node)
            if p.type in ("public_field_definition", "field_definition"):
                return text_of(p.child_by_field_name("name")) or \
                    self._anon(node)
        return self._anon(node)

    def _anon(self, node: Node) -> str:
        return f"<anon@L{node.start_point[0] + 1}>"

    def _signature(self, node: Node, name: str) -> str:
        params = node.child_by_field_name("parameters")
        return f"{name}{text_of(params) if params is not None else '()'}"

    def _inherits(self, cls: Node, cls_id: str, out: ParseOutput) -> None:
        for c in cls.children:
            if c.type == "class_heritage":
                for h in c.children:
                    self._collect_heritage(h, cls_id, out)
            elif c.type in ("extends_type_clause", "implements_clause",
                            "extends_clause"):
                self._collect_heritage(c, cls_id, out)

    def _collect_heritage(self, node: Node, cls_id: str, out: ParseOutput) -> None:
        for n in node.children:
            if n.type in ("identifier", "type_identifier",
                          "member_expression", "generic_type",
                          "nested_type_identifier"):
                base = text_of(n).split("<")[0].strip()
                if base:
                    out.inherits.append(
                        RawInherit(cls_id, base, n.start_point[0] + 1))

    def _es_import(self, node: Node, out: ParseOutput) -> None:
        src = node.child_by_field_name("source")
        if src is None:
            return
        raw = _strip_str(src)
        line = node.start_point[0] + 1
        names: list[tuple[str, str]] = []
        for clause in node.children:
            if clause.type != "import_clause":
                continue
            for c in clause.children:
                if c.type == "identifier":       # default import
                    names.append(("default", text_of(c)))
                elif c.type == "namespace_import":
                    for x in c.children:
                        if x.type == "identifier":
                            names.append(("*", text_of(x)))
                elif c.type == "named_imports":
                    for spec in c.children:
                        if spec.type == "import_specifier":
                            nm = text_of(spec.child_by_field_name("name"))
                            alias = spec.child_by_field_name("alias")
                            local = text_of(alias) if alias is not None else nm
                            names.append((nm, local))
        out.raw_imports.append(RawImport(raw, line, "import", names=names))

    def _export_from(self, node: Node, out: ParseOutput) -> None:
        src = node.child_by_field_name("source")
        if src is None:
            return
        out.raw_imports.append(RawImport(
            _strip_str(src), node.start_point[0] + 1, "export_from"))

    def _call(self, node: Node, owner: str, out: ParseOutput) -> None:
        fn = node.child_by_field_name("function")
        args = node.child_by_field_name("arguments")
        line = node.start_point[0] + 1
        if fn is None:
            return
        if fn.type == "identifier" and text_of(fn) == "require" and args:
            s = self._first_string(args)
            if s is not None:
                out.raw_imports.append(RawImport(s, line, "require"))
            return
        if fn.type == "import" and args:
            s = self._first_string(args)
            if s is not None:
                out.raw_imports.append(RawImport(s, line, "dynamic"))
            return
        if fn.type == "identifier":
            out.calls.append(RawCall(owner, text_of(fn), None, line))
        elif fn.type == "member_expression":
            prop = fn.child_by_field_name("property")
            obj = fn.child_by_field_name("object")
            out.calls.append(RawCall(owner, text_of(prop), text_of(obj), line))

    def _new(self, node: Node, owner: str, out: ParseOutput) -> None:
        ctor = node.child_by_field_name("constructor")
        if ctor is not None and ctor.type in ("identifier", "member_expression"):
            name = text_of(ctor).split(".")[-1]
            out.calls.append(RawCall(owner, name, None,
                                     node.start_point[0] + 1))

    def _first_string(self, args: Node):
        for c in args.children:
            if c.type in ("string", "template_string"):
                return _strip_str(c)
        return None

    # -- def/use ---------------------------------------------------------
    def _defuse(self, sym_id: str, func: Node, out: ParseOutput) -> None:
        b = DefUseBuilder(sym_id)
        start = func.start_point[0] + 1
        params = func.child_by_field_name("parameters")
        if params is not None:
            idx = 0
            for c in params.children:
                for nm in self._param_names(c):
                    b.add_param(nm, start, idx)
                    idx += 1
        body = func.child_by_field_name("body")
        if body is None:
            out.defuses.extend(b.build())
            return

        stack = list(reversed(body.children)) if body.type != "identifier" \
            else [body]
        while stack:
            n = stack.pop()
            t = n.type
            if t in _SCOPE_TYPES:
                continue
            if t == "variable_declarator":
                nm = n.child_by_field_name("name")
                for tgt in self._targets(nm):
                    b.add_def(tgt, n.start_point[0] + 1)
                val = n.child_by_field_name("value")
                if val is not None:
                    for ident in self._idents(val):
                        b.add_use(text_of(ident), ident.start_point[0] + 1)
                continue
            if t in ("assignment_expression", "augmented_assignment_expression"):
                left = n.child_by_field_name("left")
                for tgt in self._targets(left):
                    b.add_def(tgt, n.start_point[0] + 1)
                right = n.child_by_field_name("right")
                if right is not None:
                    for ident in self._idents(right):
                        b.add_use(text_of(ident), ident.start_point[0] + 1)
                continue
            if t == "return_statement":
                for ident in self._idents(n):
                    b.add_return(text_of(ident), n.start_point[0] + 1)
                continue
            if t == "identifier":
                b.add_use(text_of(n), n.start_point[0] + 1)
                continue
            stack.extend(reversed(n.children))
        out.defuses.extend(b.build())

    def _param_names(self, node: Node) -> list[str]:
        t = node.type
        if t == "identifier":
            return [text_of(node)]
        if t in ("required_parameter", "optional_parameter"):
            pat = node.child_by_field_name("pattern")
            return self._targets(pat) if pat is not None else []
        if t in ("rest_pattern", "assignment_pattern"):
            return self._targets(node)
        if t in ("object_pattern", "array_pattern"):
            return self._targets(node)
        return []

    def _targets(self, node) -> list[str]:
        if node is None:
            return []
        out = []
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type in ("identifier", "shorthand_property_identifier_pattern"):
                out.append(text_of(n))
            elif n.type in ("array_pattern", "object_pattern",
                            "rest_pattern", "assignment_pattern",
                            "pair_pattern"):
                # skip the "value" side default expressions for defaults
                stack.extend(n.children)
        return out

    def _idents(self, node: Node) -> list[Node]:
        out = []
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "identifier":
                out.append(n)
            elif n.type == "member_expression":
                obj = n.child_by_field_name("object")
                if obj is not None:
                    stack.append(obj)
            else:
                stack.extend(n.children)
        return out
